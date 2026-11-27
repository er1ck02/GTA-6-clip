"""Re-pull a recorded moment from the streamer's VOD, for editing, when its source file was deleted.

A live moment's project points at ``<data>/live/<channel>_<date>_<time>/moments/mNNN_<t>s.mp4``. The session's
``source.json`` holds the stream URL and when the recorder started; ``queue.json`` holds the moment's window in
recorder seconds. This finds the VOD that was live at that time (Twitch archives, or YouTube streams), works out
the offset (recorder start - VOD start + moment start) and downloads just that section with yt-dlp, into the
same ``moments`` folder so the clipper still knows the channel (facecam box, chat smear, on-screen credit).

    python scripts/repull_moment.py m014-1613s-db078db09b --before 400 --after 30
    python scripts/repull_moment.py m014-1613s-db078db09b --dry-run

Live recordings run roughly 5-15 s behind the broadcast, so the default padding is generous; check the pulled
file's transcript against the project's before cutting. Then: ``python -m app.cli process --input <file> --top 0``,
``review --clip ... --start/--end/--cut/--title``, ``export --clips ...``. Nothing here uploads anything.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def ytdlp() -> str:
    exe = ROOT / ".venv" / "Scripts" / "yt-dlp.exe"
    return str(exe) if exe.exists() else "yt-dlp"


def run_json_lines(args: list[str]) -> list[str]:
    cp = subprocess.run([ytdlp(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return [ln for ln in cp.stdout.splitlines() if ln.strip()]


def vod_list(url: str, n: int) -> list[str]:
    """Recent VOD URLs for a channel's live URL (Twitch archives, YouTube streams tab)."""
    if "twitch.tv" in url:
        ch = url.rstrip("/").split("/")[-1]
        lst = f"https://www.twitch.tv/{ch}/videos?filter=archives&sort=time"
        ids = run_json_lines(["--flat-playlist", "--playlist-end", str(n), "--print", "%(id)s", lst])
        return [f"https://www.twitch.tv/videos/{i.lstrip('v')}" for i in ids]
    if "youtube.com" in url:
        base = url.split("/live")[0].rstrip("/")
        ids = run_json_lines(["--flat-playlist", "--playlist-end", str(n), "--print", "%(id)s", base + "/streams"])
        return [f"https://www.youtube.com/watch?v={i}" for i in ids]
    raise SystemExit(f"don't know how to list VODs for {url}")


def vod_start(vod: str) -> tuple[float, float] | None:
    """(start epoch, duration) of a VOD. YouTube's release_timestamp is the stream start."""
    out = run_json_lines(["--print", "%(release_timestamp)s|%(timestamp)s|%(duration)s", vod])
    if not out:
        return None
    rel, ts, dur = (out[-1].split("|") + ["NA", "NA", "NA"])[:3]
    start = rel if rel not in ("NA", "None", "") else ts
    try:
        return float(start), float(dur)
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("project", help="project id of the deleted moment (yt_data/projects/<id>)")
    ap.add_argument("--data", type=Path, default=ROOT / "yt_data")
    ap.add_argument("--before", type=float, default=30, help="seconds to pull before the moment's window")
    ap.add_argument("--after", type=float, default=30, help="seconds to pull after it")
    ap.add_argument("--format", default="1080p60/1080p/best", help="yt-dlp format (YouTube: e.g. 299+140)")
    ap.add_argument("--vods", type=int, default=15, help="how many recent VODs to search")
    ap.add_argument("--dry-run", action="store_true", help="print the VOD and section, download nothing")
    a = ap.parse_args()

    pj = json.loads((a.data / "projects" / a.project / "project.json").read_text(encoding="utf-8"))
    src = Path(pj["source"]["path"])
    session, mid = src.parent.parent, src.stem
    source = json.loads((session / "source.json").read_text(encoding="utf-8"))
    queue = json.loads((session / "queue.json").read_text(encoding="utf-8"))
    m = next((x for x in queue["moments"] if x["id"] == mid), None)
    if m is None:
        raise SystemExit(f"{mid} not in {session / 'queue.json'}")
    lo, hi = m.get("saved") or m["window"]
    started = float(source["started"])

    for vod in vod_list(source["url"], a.vods):
        info = vod_start(vod)
        if info and info[0] <= started <= info[0] + info[1]:
            break
    else:
        raise SystemExit(f"no VOD among the last {a.vods} covers the recording (it may be deleted or sub-only)")
    v0, vdur = info
    s = max(0.0, started - v0 + lo - a.before)
    e = min(vdur, started - v0 + hi + a.after)
    out = session / "moments" / f"r_{mid}_vod{round(s)}.mp4"
    print(json.dumps({"vod": vod, "vod_start": v0, "section": [round(s, 1), round(e, 1)], "out": str(out),
                      "moment_in_file": [round(lo - (s - (started - v0)), 1), round(hi - (s - (started - v0)), 1)]},
                     indent=1))
    if a.dry_run:
        return 0
    cmd = [ytdlp(), "-f", a.format, "--download-sections", f"*{s:.1f}-{e:.1f}", "--merge-output-format", "mp4",
           "-o", str(out), vod]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
