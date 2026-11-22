"""Optional LLM re-ranking of highlight candidates, with a provider list and fallbacks.

Providers, tried in order until one returns a valid answer:

* ``anthropic``: Claude through the official ``anthropic`` SDK (``pip install -r requirements/llm.txt``),
  structured JSON output, server-side refusal fallback enabled.
* ``openai_compatible``: any ``/chat/completions`` endpoint (OpenAI, Groq, OpenRouter, LM Studio, vLLM...).
* ``ollama``: a local Ollama server (``/api/chat`` with a JSON schema).

Keys are read from the environment variable named in the config, never stored.
The model only re-orders and re-titles; boundaries stay with the heuristic, and
the answer is validated (unknown ids are ignored, missing ids keep their score).
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Callable

from pydantic import BaseModel, Field, ValidationError

from clipstudio.config import LLMCfg, LLMProvider
from clipstudio.core.logging import get_logger
from clipstudio.core.models import Candidate, CandidateSet

log = get_logger("llm")

SYSTEM = (
    "You are a short-form video editor who picks gaming stream highlights for TikTok, YouTube Shorts and Reels. "
    "You get candidate clips from one stream: their transcript, timing and measured signals (audio loudness, "
    "on-screen events such as death screens or explosions, chat activity). Judge each one as a standalone short: "
    "is there a clear moment (setup, payoff, reaction), would it hook a viewer in the first two seconds without "
    "context, is it funny, surprising or impressive? Penalise dead air, filler talk, menus and clips that only make "
    "sense with earlier context. Score 0-10. The title is shown on the video and used as the post title, so write it the way a fan types a caption: all lowercase, 3 to 8 words, exactly one emoji at the end (prefer \U0001F62D, then \U0001F480 or \U0001F633), name the streamer the way fans write the name when it helps, and do not give away the punchline. Good: 'what did bro just do \U0001F62D', 'ludwig cannot drive \U0001F62D', 'he had one job \U0001F480', 'gta 6 npcs are not real \U0001F62D', 'wait gta 6 has this?? \U0001F633', or a real 2 to 6 word quote from the clip plus the emoji. Never Title Case, never ALL CAPS, no labels like '(full crash)', no ad-style questions, no hashtags. Set keep to false unless all four hold: a visible game event or a quotable line; a loud streamer reaction within about two seconds of it; it makes sense in under five seconds with no backstory; the streamer is on camera. Reject talk with no event, menus and loading screens, long drives, chat reading and sponsor segments. "
    "Also give a one-line hook for the post text and a short "
    "reason. Return every candidate id exactly once."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "integer"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "title", "hook", "keep", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}


class Judgement(BaseModel):
    id: str
    score: int = Field(ge=0, le=10)
    title: str
    hook: str
    keep: bool
    reason: str


class Answer(BaseModel):
    clips: list[Judgement]


class ProviderError(RuntimeError):
    pass


def _payload(cands: list[Candidate]) -> str:
    items = []
    for c in cands:
        items.append({
            "id": c.id, "start": round(c.start, 1), "duration": round(c.duration, 1), "category": c.category,
            "transcript": c.transcript[:1500] or "(no speech)",
            "signals": {k: round(v, 2) for k, v in c.features.items()
                        if k not in ("mean_word_probability", "silence_fraction")},
            "heuristic_score": round(c.score, 3), "heuristic_reason": c.reason,
        })
    return "Candidates:\n" + json.dumps(items, ensure_ascii=False)


def _key(p: LLMProvider) -> str | None:
    if not p.api_key_env:
        return None
    k = os.environ.get(p.api_key_env)
    return k or None


def _call_anthropic(p: LLMProvider, cfg: LLMCfg, prompt: str) -> str:
    try:
        import anthropic
    except ImportError:
        return _call_anthropic_http(p, cfg, prompt)  # no SDK installed: plain HTTPS, same request
    key = _key(p)
    client = anthropic.Anthropic(api_key=key, timeout=cfg.timeout_seconds) if key else \
        anthropic.Anthropic(timeout=cfg.timeout_seconds)  # SDK resolves other credentials itself
    try:
        resp = client.beta.messages.create(
            model=p.model,
            max_tokens=16000,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": SCHEMA}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError as e:
        raise ProviderError(f"Anthropic authentication failed (set {p.api_key_env})") from e
    except anthropic.APIStatusError as e:
        raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise ProviderError(f"cannot reach the Anthropic API: {e}") from e
    if resp.stop_reason == "refusal":
        raise ProviderError("the model declined the request")
    if resp.stop_reason == "max_tokens":
        raise ProviderError("the answer was cut off (max_tokens)")
    text = next((b.text for b in resp.content if getattr(b, "type", "") == "text"), None)
    if not text:
        raise ProviderError("empty answer")
    return text


def _call_anthropic_http(p: LLMProvider, cfg: LLMCfg, prompt: str) -> str:
    """The Messages API over plain HTTPS, for installs without the ``anthropic`` package."""
    key = _key(p)
    if not key:
        raise ProviderError(f"no API key ({p.api_key_env} is not set) and the anthropic package is not installed")
    base = (p.base_url or "https://api.anthropic.com").rstrip("/")
    d = _post(f"{base}/v1/messages", {
        "model": p.model, "max_tokens": 16000, "system": SYSTEM,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}},
    }, {"x-api-key": key, "anthropic-version": "2023-06-01"}, cfg.timeout_seconds)
    if d.get("stop_reason") in ("refusal", "max_tokens"):
        raise ProviderError(f"no usable answer (stop_reason {d.get('stop_reason')})")
    text = next((b.get("text") for b in d.get("content", []) if b.get("type") == "text"), None)
    if not text:
        raise ProviderError(f"unexpected response shape: {str(d)[:200]}")
    return text


def _post(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001 - network, HTTP and JSON errors all mean "try the next provider"
        raise ProviderError(f"{url}: {e}") from e


def _call_openai(p: LLMProvider, cfg: LLMCfg, prompt: str) -> str:
    base = (p.base_url or "https://api.openai.com/v1").rstrip("/")
    key = _key(p)
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    d = _post(f"{base}/chat/completions", {
        "model": p.model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "clip_ranking", "schema": SCHEMA, "strict": True}},
    }, headers, cfg.timeout_seconds)
    try:
        return d["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderError(f"unexpected response shape: {str(d)[:200]}") from e


def _call_ollama(p: LLMProvider, cfg: LLMCfg, prompt: str) -> str:
    base = (p.base_url or "http://localhost:11434").rstrip("/")
    d = _post(f"{base}/api/chat", {
        "model": p.model, "stream": False, "format": SCHEMA, "options": {"temperature": 0.2},
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
    }, {}, cfg.timeout_seconds)
    try:
        return d["message"]["content"]
    except (KeyError, TypeError) as e:
        raise ProviderError(f"unexpected response shape: {str(d)[:200]}") from e


CALLERS: dict[str, Callable[[LLMProvider, LLMCfg, str], str]] = {
    "anthropic": _call_anthropic, "openai_compatible": _call_openai, "ollama": _call_ollama,
}


def judge(cands: list[Candidate], cfg: LLMCfg) -> tuple[Answer, str]:
    """Ask providers in order; returns the validated answer and 'kind:model' of the provider that gave it."""
    prompt = _payload(cands)
    errors = []
    for p in cfg.providers:
        name = f"{p.kind}:{p.model}"
        try:
            text = CALLERS[p.kind](p, cfg, prompt)
            ans = Answer.model_validate_json(text)
            if not ans.clips:
                raise ProviderError("no clips in the answer")
            log.info("LLM re-rank by %s: %d judgements", name, len(ans.clips))
            return ans, name
        except (ProviderError, ValidationError, ValueError) as e:
            msg = str(e).splitlines()[0][:200]
            log.warning("LLM provider %s failed: %s", name, msg)
            errors.append(f"{name}: {msg}")
    raise ProviderError("all LLM providers failed: " + "; ".join(errors))


def rerank(cs: CandidateSet, cfg: LLMCfg, minimum_score: float) -> tuple[CandidateSet, str]:
    """Blend LLM scores into the top-k candidates and re-sort. Returns (new set, note)."""
    top = cs.candidates[: cfg.top_k]
    if not top:
        return cs, "no candidates to re-rank"
    ans, used = judge(top, cfg)
    by_id = {j.id: j for j in ans.clips}
    top_ids = {t.id for t in top}
    out = []
    for c in cs.candidates:
        c = c.model_copy(deep=True)
        j = by_id.get(c.id)
        if j is not None and c.id in top_ids:
            heur = c.score
            c.features["llm_score"] = round(j.score / 10, 3)
            c.features["llm_keep"] = 1.0 if j.keep else 0.0
            c.features["heuristic_score"] = round(heur, 4)
            c.score = round((1 - cfg.weight) * heur + cfg.weight * (j.score / 10), 4)
            if not j.keep:
                c.score = round(c.score * 0.7, 4)
            c.passes_threshold = c.score >= minimum_score and c.passes_threshold and j.keep
            if cfg.retitle and j.title.strip():
                c.title = j.title.strip()[:80]
                c.title_source = "llm"
            c.reason = f"{c.reason}; LLM ({used}) {j.score}/10: {j.reason.strip()}"
            if j.hook.strip():
                c.description = f"Hook: {j.hook.strip()} | {c.description}"
        out.append(c)
    out.sort(key=lambda c: -c.score)
    for i, c in enumerate(out, 1):
        c.rank = i
    return CandidateSet(weights=cs.weights, candidates=out), f"re-ranked top {len(top)} with {used}"
