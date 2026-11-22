"""Live mode on a real encode played back in real time: the buffer stays bounded and the moment is kept."""
import json
import subprocess
import threading
import time

import pytest

from clipstudio.config import load_config
from clipstudio.live.buffer import Recorder
from clipstudio.live.chat import ChatSource, ReplayChat
from clipstudio.live.session import LiveSession
from clipstudio.media.ffmpeg import probe

pytestmark = pytest.mark.integration


def _stream(path, dur, loud_at):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=30:duration={dur}",
                    "-f", "lavfi", "-i", f"sine=frequency=300:sample_rate=48000:duration={dur}", "-filter_complex",
                    f"[1:a]volume='if(between(t,{loud_at},{loud_at + 3}),0.9,0.05)':eval=frame[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast", "-g", "30",
                    "-c:a", "aac", str(path)], check=True)
    return path


def test_realtime_buffer_is_bounded_and_moment_is_saved(tmp_path):
    src = _stream(tmp_path / "s.mp4", 50, 38)
    cfg = load_config(overrides=["live.buffer_seconds=20", "live.pre_seconds=10", "live.post_seconds=6",
                                 "live.warmup_seconds=15", "live.segment_seconds=2"])
    rec = Recorder(tmp_path / "buf", 2.0, file=src, realtime=True)
    sess = LiveSession(cfg, rec, ChatSource(), tmp_path / "out", channel="test", process=False)
    counts, stop = [], threading.Event()

    def watch():
        while not stop.is_set():
            counts.append(len(list((tmp_path / "buf").glob("seg_*.ts"))))
            time.sleep(0.5)

    threading.Thread(target=watch, daemon=True).start()
    moments = sess.run()
    stop.set()
    assert len(moments) == 1 and moments[0].status == "saved"
    m = moments[0]
    assert 36 <= m.trigger["t"] <= 40
    assert m.saved[0] <= m.trigger["t"] - 9 and m.saved[1] >= m.trigger["t"] + 5
    info = probe(__import__("pathlib").Path(m.path))
    assert abs(info.duration - (m.saved[1] - m.saved[0])) < 0.6 and info.has_audio
    # never more than the buffer (20 s = 10 segments) plus the pinned moment and the segment being written
    assert max(counts) <= 10 + 9 + 2, counts
    assert sess.ring.deleted > 0
    assert list((tmp_path / "buf").glob("seg_*.ts")) == []  # only the moment survives the session
    q = json.loads((tmp_path / "out" / "queue.json").read_text())
    assert q["moments"][0]["id"] == m.id and q["moments"][0]["upload"] == "review"


def test_chat_replay_moment_carries_its_chat(tmp_path):
    src = _stream(tmp_path / "s.mp4", 90, 60)
    chat = [{"time_in_seconds": float(t), "message": "hi", "author": {"name": "u"}} for t in range(0, 90, 3)]
    chat += [{"time_in_seconds": 61 + i * 0.1, "message": "KEKW no way", "author": {"name": f"u{i}"}} for i in range(25)]
    (tmp_path / "s.chat.json").write_text(json.dumps(chat))
    cfg = load_config(overrides=["live.pre_seconds=20", "live.post_seconds=10"])
    rec = Recorder(tmp_path / "buf", 2.0, file=src, realtime=False)
    sess = LiveSession(cfg, rec, ReplayChat(tmp_path / "s.chat.json"), tmp_path / "out", process=False)
    [m] = sess.run()
    assert {"chat", "hype"} <= set(m.trigger["reasons"]) and 57 <= m.trigger["t"] <= 63
    rows = json.loads(open(m.chat_path).read())
    assert sum(r["message"] == "KEKW no way" for r in rows) == 25
    assert all(0 <= r["time_in_seconds"] <= m.saved[1] - m.saved[0] for r in rows)


def test_split_video_audio_urls_and_reconnect(tmp_path, monkeypatch):
    """YouTube live offers video and audio as separate HLS streams; and media URLs expire, so a dropped
    connection is reopened with stream time continuing."""
    import functools
    import http.server

    from clipstudio.live import buffer as buf_mod

    src = _stream(tmp_path / "s.mp4", 12, 100)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:v", "-c", "copy", str(tmp_path / "v.ts")],
                   check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-map", "0:a", "-c", "copy", str(tmp_path / "a.ts")],
                   check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    _which = buf_mod.shutil.which
    monkeypatch.setattr(buf_mod.shutil, "which", lambda n: None if n == "streamlink" else _which(n))  # yt-dlp path
    monkeypatch.setattr(buf_mod, "resolve_stream_urls", lambda url, max_height=1080: [f"{base}/v.ts", f"{base}/a.ts"])
    over = iter([False, True])
    monkeypatch.setattr(LiveSession, "_stream_over", lambda self: next(over))
    cfg = load_config()
    rec = Recorder(tmp_path / "buf", 2.0, url="https://example.invalid/live")
    sess = LiveSession(cfg, rec, ChatSource(), tmp_path / "out", process=False)
    try:
        sess.run()
    finally:
        srv.shutdown()
    assert rec.restarts == 1
    assert 20 <= sess.ring.head <= 26  # two 12 s passes, stream time continuing across the reconnect
    starts = [s.start for s in sess.ring.segments]
    assert starts == sorted(starts) and len(set(s.index for s in sess.ring.segments)) == len(starts)


def test_unreachable_stream_is_an_error(tmp_path, monkeypatch):
    from clipstudio.live import buffer as buf_mod
    from clipstudio.live.session import LiveError

    _which = buf_mod.shutil.which
    monkeypatch.setattr(buf_mod.shutil, "which", lambda n: None if n == "streamlink" else _which(n))
    monkeypatch.setattr(buf_mod, "resolve_stream_urls", lambda url, max_height=1080: ["http://127.0.0.1:9/nothing.m3u8"])
    rec = Recorder(tmp_path / "buf", 2.0, url="https://example.invalid/live")
    sess = LiveSession(load_config(), rec, ChatSource(), tmp_path / "out", process=False)
    with pytest.raises(LiveError, match="never started"):
        sess.run()
