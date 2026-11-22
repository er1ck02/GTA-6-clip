"""LLM re-ranker: provider fallbacks, response validation and score blending (no real network or keys)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from clipstudio.config import LLMCfg, LLMProvider
from clipstudio.core.models import Candidate, CandidateSet
from clipstudio.highlights import llm


def _cand(i, score):
    return Candidate(id=f"c{i}", rank=i, start=i * 30.0, end=i * 30.0 + 20, duration=20, peak_time=i * 30.0 + 5,
                     title=f"t{i}", description="d", category="reaction", score=score, passes_threshold=True,
                     confidence=0.8, features={"audio_intensity": 0.5}, contributions={}, reason="r",
                     transcript=f"words {i}")


def _set():
    return CandidateSet(weights={"audio_intensity": 1.0}, candidates=[_cand(1, 0.8), _cand(2, 0.7), _cand(3, 0.6)])


def _answer(scores):
    return json.dumps({"clips": [{"id": k, "score": v, "title": f"T {k}", "hook": "h", "keep": True, "reason": "why"}
                                 for k, v in scores.items()]})


class _Server:
    def __init__(self, reply):
        outer = self
        self.bodies = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers["Content-Length"])
                outer.bodies.append((self.path, json.loads(self.rfile.read(n)), dict(self.headers)))
                data = reply(self.path).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def test_ollama_reorders_and_retitles():
    srv = _Server(lambda path: json.dumps({"message": {"content": _answer({"c1": 2, "c2": 6, "c3": 10})}}))
    try:
        cfg = LLMCfg(enabled=True, providers=[LLMProvider(kind="ollama", model="m", base_url=srv.url)], weight=0.5)
        out, note = llm.rerank(_set(), cfg, 0.55)
    finally:
        srv.close()
    assert [c.id for c in out.candidates] == ["c3", "c2", "c1"]
    assert out.candidates[0].title == "T c3" and out.candidates[0].features["llm_score"] == 1.0
    assert out.candidates[0].score == pytest.approx(0.5 * 0.6 + 0.5 * 1.0)
    assert srv.bodies[0][0] == "/api/chat" and srv.bodies[0][1]["format"] == llm.SCHEMA
    assert "ollama:m" in note


def test_falls_back_to_next_provider_and_ignores_unknown_ids(monkeypatch):
    srv = _Server(lambda path: json.dumps({"choices": [{"message": {"content": _answer({"c2": 10, "zzz": 10})}}]}))
    monkeypatch.setenv("TEST_LLM_KEY", "sk-test")
    try:
        cfg = LLMCfg(enabled=True, providers=[
            LLMProvider(kind="ollama", model="down", base_url="http://127.0.0.1:9"),  # nothing listens
            LLMProvider(kind="openai_compatible", model="m2", base_url=srv.url, api_key_env="TEST_LLM_KEY")])
        out, note = llm.rerank(_set(), cfg, 0.55)
    finally:
        srv.close()
    assert "openai_compatible:m2" in note
    assert srv.bodies[0][2]["Authorization"] == "Bearer sk-test"
    ids = [c.id for c in out.candidates]
    assert ids[0] == "c2" and set(ids) == {"c1", "c2", "c3"}
    assert "llm_score" not in next(c for c in out.candidates if c.id == "c1").features  # not judged: unchanged


def test_invalid_answers_fail_cleanly():
    srv = _Server(lambda path: json.dumps({"message": {"content": '{"clips": [{"id": "c1", "score": 99}]}'}}))
    try:
        cfg = LLMCfg(enabled=True, providers=[LLMProvider(kind="ollama", model="m", base_url=srv.url)])
        with pytest.raises(llm.ProviderError):
            llm.rerank(_set(), cfg, 0.55)
    finally:
        srv.close()


def test_anthropic_request_shape(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    seen = {}

    class Block:
        type = "text"
        text = _answer({"c1": 7, "c2": 3, "c3": 5})

    class Resp:
        stop_reason = "end_turn"
        content = [Block()]

    class Msgs:
        def create(self, **kw):
            seen.update(kw)
            return Resp()

    class Fake:
        def __init__(self, **kw):
            seen["client"] = kw
            self.beta = type("B", (), {"messages": Msgs()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = LLMCfg(enabled=True, providers=[LLMProvider(kind="anthropic", model="claude-opus-5-5",
                                                      api_key_env="ANTHROPIC_API_KEY")])
    out, note = llm.rerank(_set(), cfg, 0.55)
    assert seen["model"] == "claude-opus-5-5" and seen["output_config"]["format"]["schema"] == llm.SCHEMA
    assert seen["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["betas"]
    assert "temperature" not in seen and "thinking" not in seen
    assert out.candidates[0].id == "c1"


def test_refusal_moves_to_next_provider(monkeypatch):
    anthropic = pytest.importorskip("anthropic")

    class Resp:
        stop_reason = "refusal"
        content = []

    class Fake:
        def __init__(self, **kw):
            self.beta = type("B", (), {"messages": type("M", (), {"create": lambda self, **k: Resp()})()})()

    monkeypatch.setattr(anthropic, "Anthropic", Fake)
    cfg = LLMCfg(enabled=True, providers=[LLMProvider(kind="anthropic", model="claude-opus-5-5")])
    with pytest.raises(llm.ProviderError, match="declined"):
        llm.rerank(_set(), cfg, 0.55)


def test_llm_auto_turns_on_with_a_key(monkeypatch):
    from clipstudio.config import load_config
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = load_config().llm
    assert cfg.enabled == "auto" and not cfg.active()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert cfg.active()
    assert not load_config(overrides=["llm.enabled=false"]).llm.active()
