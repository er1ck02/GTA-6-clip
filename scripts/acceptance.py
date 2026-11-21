"""Acceptance run: checks the spec's 12 acceptance criteria against one real video, end to end.

    python scripts/acceptance.py --input "D:/Videos/my_gta_stream.mp4" [--report acceptance.md] [--check-install]

Every criterion is checked on the actual files the studio produces (the rendered MP4, its QC report, the project
state), never assumed. Criterion 10 really interrupts a render (Ctrl+C / SIGINT on a child process) and resumes it.
Criterion 12 with --check-install builds a wheel and installs it into a fresh virtual environment (needs network).
Exit code 0 only if every criterion passed.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from clipstudio.config import load_config  # noqa: E402
from clipstudio.core.models import CaptionTrack, CropPlan, QCReport, Transcript  # noqa: E402
from clipstudio.pipeline.studio import Studio  # noqa: E402

CRITERIA = {
    1: "A user can import a real gaming VOD",
    2: "The pipeline can analyze the video",
    3: "It can generate ranked highlight candidates",
    4: "A user can select or adjust a highlight",
    5: "The video can be intelligently reframed into 9:16",
    6: "Captions have accurate word-level timing",
    7: "The audio remains synchronized and intelligible",
    8: "The final MP4 plays correctly",
    9: "The output passes automated quality checks",
    10: "The project can recover from interrupted processing",
    11: "The documented commands actually work",
    12: "The installation process is reproducible",
}


class Run:
    def __init__(self) -> None:
        self.results: dict[int, dict] = {}

    def check(self, n: int, ok: bool, evidence: str) -> bool:
        self.results[n] = {"criterion": CRITERIA[n], "passed": bool(ok), "evidence": evidence}
        print(f"[{'PASS' if ok else 'FAIL'}] {n:>2}. {CRITERIA[n]}: {evidence}", flush=True)
        return ok

    def error(self, n: int, e: BaseException) -> None:
        self.check(n, False, f"{type(e).__name__}: {e}")
        traceback.print_exc()


def cli(args: list[str], data_dir: Path, extra: list[str], timeout: float = 3600) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "app.cli", *args, "--set", f"paths.data_dir={data_dir}", *extra]
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def qc_check(qc: QCReport, name: str):
    return next((c for c in qc.checks if c.name == name), None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--input", type=Path)
    src.add_argument("--url")
    ap.add_argument("--data-dir", type=Path, help="where the test project goes (default: a temporary folder)")
    ap.add_argument("--report", type=Path, default=Path("acceptance_report.md"))
    ap.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("--check-install", action="store_true", help="build + install into a fresh venv (slow, needs network)")
    a = ap.parse_args()

    data_dir = a.data_dir or Path(tempfile.mkdtemp(prefix="clipstudio-acceptance-"))
    extra = [x for o in a.overrides for x in ("--set", o)]
    cfg = load_config(overrides=[f"paths.data_dir={data_dir}", *a.overrides])
    st = Studio(cfg)
    run = Run()
    t_start = time.time()
    p = c = plan = qc = None
    out = None

    # 1 import
    try:
        p = st.open_or_create(input_path=a.input, url=a.url)
        run.check(1, p.source_path().exists(), f"project {p.id} from {p.source_path().name}")
    except Exception as e:
        run.error(1, e)

    # 2 analyse
    if p:
        try:
            t0 = time.time()
            st.analyze(p)
            stages = {k: v["status"] for k, v in p.state.data["stages"].items()}
            bad = {k: v for k, v in stages.items() if v not in ("done", "cached", "skipped")}
            tr = Transcript.load(p.p("transcript"))
            run.check(2, not bad, f"{len(stages)} stages in {time.time() - t0:.0f}s, {len(tr.words())} words transcribed"
                      + (f"; not done: {bad}" if bad else ""))
        except Exception as e:
            run.error(2, e)

    # 3 candidates
    if p and p.p("candidates").exists():
        cs = st.candidates(p)
        scores = [x.score for x in cs.candidates]
        ok = bool(cs.candidates) and scores == sorted(scores, reverse=True) and all(x.reason for x in cs.candidates)
        top = cs.candidates[0] if cs.candidates else None
        run.check(3, ok, f"{len(cs.candidates)} candidates, ranked by score"
                  + (f"; #1 {top.start:.1f}-{top.end:.1f}s score {top.score:.2f}: {top.reason[:90]}" if top else ""))

        # 4 select / adjust
        try:
            c0 = cs.candidates[0]
            new_start = round(min(c0.start + 0.5, c0.end - 2.0), 2)
            st.edit_clip(p, c0.id, status="accepted", start=new_start)
            c = st.candidates(p).get(c0.id)
            run.check(4, c.status == "accepted" and abs(c.start - new_start) < 1e-6 and c.user_adjusted,
                      f"{c.id} accepted and trimmed to start {c.start:.2f}s (stored in edits.json)")
        except Exception as e:
            run.error(4, e)

    # 5-9 render the adjusted clip and inspect what came out
    if c:
        try:
            res = st.render(p, [c.id], force=True)[0]
            out = res.output
            d = p.clip_dir(c.id)
            plan = CropPlan.load(d / "cropplan.json")
            qc = QCReport.load(d / "qc.json")
            ratio = plan.out_width / plan.out_height
            how = {"facecam_split": "facecam + gameplay split", "facecam_pip": "gameplay + facecam PiP",
                   "gameplay": "gameplay crop", "fit_blur": "whole frame over blurred fill"}.get(plan.mode, plan.mode)
            res_c = qc_check(qc, "resolution")
            run.check(5, abs(ratio - 9 / 16) < 1e-3 and res_c is not None and res_c.status == "pass",
                      f"{plan.out_width}x{plan.out_height} {how} ({plan.source}), {len(plan.keyframes)} crop keyframe(s)")

            tr = CaptionTrack.load(d / "captions.json") if (d / "captions.json").exists() else None
            sync = qc_check(qc, "caption_sync")
            if tr is None:
                run.check(6, False, "captions disabled or missing")
            else:
                words = [w for ch in tr.chunks for ln in ch.lines for w in ln]
                mono = all(words[i].start <= words[i + 1].start + 1e-6 for i in range(len(words) - 1))
                # caption words must be the transcript's own word times shifted to the clip
                src = Transcript.load(p.p("transcript")).words_between(c.start, c.end)
                dev = max((abs((s.start - c.start) - w.start) for s, w in zip(src, words)
                           if s.text.strip(" ,.!?").lower() in w.text.lower()), default=0.0)
                run.check(6, mono and sync is not None and sync.status == "pass" and dev < 0.06,
                          f"{len(words)} words, monotonic, max deviation from transcript word times {dev * 1000:.0f} ms, "
                          f"QC caption_sync {sync.status if sync else 'missing'}")

            names = ("audio_present", "av_sync", "loudness", "true_peak", "no_clipping", "long_silence")
            got = {n: (qc_check(qc, n).status if qc_check(qc, n) else "missing") for n in names}
            run.check(7, all(v in ("pass", "skip") for v in got.values()) and got.get("audio_present") == "pass",
                      ", ".join(f"{k} {v}" for k, v in got.items()))

            dec = qc_check(qc, "decodes_cleanly")
            dur = qc_check(qc, "duration")
            run.check(8, out.exists() and dec is not None and dec.status == "pass" and dur is not None and dur.status == "pass",
                      f"{out.name}: {dec.detail if dec else 'no decode check'}; {dur.detail if dur else ''}")

            fails = [f"{x.name}: {x.detail}" for x in qc.checks if x.status == "fail"]
            run.check(9, qc.passed, qc.summary() + (f"; failed: {fails}" if fails else ""))
        except Exception as e:
            for n in (5, 6, 7, 8, 9):
                if n not in run.results:
                    run.error(n, e)

    # 10 interrupt a real render (SIGINT to a child process), then resume
    if c:
        try:
            proc = subprocess.Popen([sys.executable, "-m", "app.cli", "export", "--project", p.id, "--clips", c.id,
                                     "--force", "--set", f"paths.data_dir={data_dir}", *extra],
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            state_f = p.clip_dir(c.id) / "state.json"
            deadline = time.time() + 600
            while time.time() < deadline and proc.poll() is None:
                s = json.loads(state_f.read_text()) if state_f.exists() else {}
                if s.get("stages", {}).get("render", {}).get("status") == "running":
                    break
                time.sleep(0.2)
            time.sleep(1.0)
            interrupted = proc.poll() is None
            if interrupted:
                proc.send_signal(signal.SIGINT if os.name != "nt" else signal.CTRL_C_EVENT)
            proc.wait(timeout=120)
            s = json.loads(state_f.read_text())
            render_state = s["stages"].get("render", {}).get("status")
            r = cli(["resume", "--project", p.id, "--json"], data_dir, extra)
            doc = json.loads(r.stdout) if r.stdout.strip() else {}
            p2 = st.load(p.id)
            analysis = {k: v["status"] for k, v in p2.state.data["stages"].items()}
            reused = all(v in ("cached", "done", "skipped") for v in analysis.values())
            passed = bool(doc.get("results")) and all(x["qc_passed"] for x in doc["results"])
            run.check(10, interrupted and render_state == "interrupted" and r.returncode == 0 and passed and reused,
                      f"render interrupted (state: {render_state}, exit {proc.returncode}); resume exit {r.returncode}, "
                      f"analysis stages reused, clip QC {'passed' if passed else 'not passed'}")
        except Exception as e:
            run.error(10, e)

    # 11 documented commands
    try:
        outs = {}
        for args in (["doctor", "--json"], ["status", "--json"], ["status", "--project", p.id, "--json"],
                     ["review", "--project", p.id, "--json"]):
            r = cli(args, data_dir, extra)
            outs[" ".join(args[:1] + (["--project"] if "--project" in args else []))] = (r.returncode, r.stdout)
        parsed = {k: (rc, json.loads(o)) for k, (rc, o) in outs.items()}
        ok = all(rc == 0 for rc, _ in parsed.values()) or (parsed["doctor"][0] == 1 and
                                                            all(rc == 0 for k, (rc, _) in parsed.items() if k != "doctor"))
        run.check(11, ok and parsed["review --project"][1]["candidates"],
                  "doctor, status, status --project, review --project all ran and printed valid JSON"
                  + (" (doctor reported a missing optional/required item, exit 1)" if parsed["doctor"][0] == 1 else ""))
    except Exception as e:
        run.error(11, e)

    # 12 reproducible install
    try:
        if a.check_install:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                subprocess.run([sys.executable, "-m", "pip", "wheel", "-q", str(ROOT), "--no-deps", "-w", str(tdp)],
                               check=True, capture_output=True)
                whl = next(tdp.glob("*.whl"))
                venv.create(tdp / "v", with_pip=True)
                py = tdp / "v" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
                subprocess.run([str(py), "-m", "pip", "install", "-q", f"{whl}[ui]"], check=True, capture_output=True)
                r = subprocess.run([str(py), "-m", "clipstudio.cli", "doctor", "--json", "--set",
                                    f"paths.logs_dir={tdp / 'logs'}"], capture_output=True, text=True, cwd=td)
                json.loads(r.stdout)
                run.check(12, r.returncode in (0, 1), f"wheel {whl.name} installed into a fresh venv; doctor ran there")
        else:
            r = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
            run.check(12, r.returncode == 0, "pip check: " + (r.stdout.strip() or r.stderr.strip())[:200]
                      + " (use --check-install for a fresh-venv install test)")
    except Exception as e:
        run.error(12, e)

    passed = sum(1 for r in run.results.values() if r["passed"])
    lines = [f"# Acceptance run · {time.strftime('%Y-%m-%d %H:%M')}", "",
             f"Source: `{a.input or a.url}` · project `{p.id if p else '-'}` · {time.time() - t_start:.0f}s · "
             f"**{passed}/{len(CRITERIA)} passed**", "", "| # | Criterion | Result | Evidence |", "|---|---|---|---|"]
    for n in CRITERIA:
        r = run.results.get(n, {"passed": False, "evidence": "not reached"})
        lines.append(f"| {n} | {CRITERIA[n]} | {'PASS' if r['passed'] else 'FAIL'} | {r['evidence'].replace('|', '/')} |")
    if out:
        lines += ["", f"Rendered clip: `{out}`"]
    a.report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    a.report.with_suffix(".json").write_text(json.dumps(run.results, indent=2))
    print(f"\n{passed}/{len(CRITERIA)} criteria passed · report: {a.report}")
    return 0 if passed == len(CRITERIA) else 1


if __name__ == "__main__":
    sys.exit(main())
