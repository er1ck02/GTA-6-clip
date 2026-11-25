"""Original formats: build a tiny top 5 episode and a news Short from synthetic clips (silent voice)."""
import json
import subprocess
from pathlib import Path

import pytest

from clipstudio.formats import meta, news, top5
from clipstudio.media.ffmpeg import probe

pytestmark = pytest.mark.integration


def _split_clip(path: Path, secs: float = 3.0, color: str = "red") -> Path:
    """A 1080x1920 split-screen clip like Clip Studio's: 'face' on top, 16:9 'gameplay' below."""
    subprocess.run(["ffmpeg", "-v", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c={color}:s=1080x960:r=30:d={secs}",
                    "-f", "lavfi", "-i", f"testsrc2=s=1080x608:r=30:d={secs}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={secs}:sample_rate=48000",
                    "-filter_complex", "[0:v][1:v]vstack,pad=1080:1920:0:0[v]",
                    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def _spec(tmp: Path, n: int = 3) -> Path:
    clips = [_split_clip(tmp / f"c{i}.mp4", color=c) for i, c in enumerate(["red", "blue", "green"][:n])]
    spec = {"series": "Top 5 GTA 6 streamer moments", "week": "Week of Nov 24, 2026",
            "title": "top 3 test moments 😭", "hook": "test", "intro": "Welcome back to the countdown.",
            "outro": "That's the week. See you next time.", "encoder": "libx264",
            "moments": [{"rank": n - i, "clip": c.name, "streamer": f"Streamer{i}", "link": f"youtube.com/@s{i}",
                         "label": "a moment", "setup": "Here is the setup line.", "react": "And that is why.",
                         "short_title": f"streamer{i} did a thing 😭", "mask": [[700, 960, 300, 200]]} for i, c in enumerate(clips)]}
    p = tmp / "ep.json"
    p.write_text(json.dumps(spec))
    return p


def test_top5_builds_episode_shorts_and_upload(tmp_path):
    res = top5.build(_spec(tmp_path), tmp_path / "out", voice_override={"engines": ["silent"]})
    ep = probe(Path(res["episode"]))
    assert (ep.width, ep.height) == (1920, 1080)
    assert ep.duration > 3 * 3          # three clips plus narration
    assert res["chapters"][0][0] == "0:00" and res["chapters"][-1][1] == "Credits"
    up = json.loads((tmp_path / "out" / "upload.json").read_text())
    assert up["visibility"] == "unlisted" and up["tags"] and "Streamer0: https://youtube.com/@s0" in up["description"]
    assert "never @wettoefungus" in up["channel"]
    assert len(res["shorts"]) == 3
    s = probe(Path(res["shorts"][0]))
    assert (s.width, s.height) == (1080, 1920)
    assert Path(res["thumbnail"]).exists()


def test_top5_refuses_rp_and_todo(tmp_path):
    p = _spec(tmp_path)
    spec = json.loads(p.read_text())
    spec["moments"][0]["setup"] = "best gta rp moment"
    p.write_text(json.dumps(spec))
    with pytest.raises(meta.RuleError):
        top5.build(p, tmp_path / "o", voice_override={"engines": ["silent"]})
    spec["moments"][0]["setup"] = "TODO"
    p.write_text(json.dumps(spec))
    with pytest.raises(meta.RuleError):
        top5.build(p, tmp_path / "o", voice_override={"engines": ["silent"]})


def _news_spec(tmp: Path, cite: bool = True) -> Path:
    _split_clip(tmp / "g.mp4", secs=4)
    spec = {"title": "gta 6 got its first patch 😳", "kicker": "gta 6 news", "slug": "patch",
            "sources": {"a": {"name": "Example News, Nov 23", "url": "https://example.com/a"}},
            "footage": [{"streamer": "Streamer0", "link": "youtube.com/@s0"}], "clip": "g.mp4",
            "beats": [{"say": "GTA 6 just got its first patch.", "show": "first patch is out",
                       **({"sources": ["a"]} if cite else {})},
                      {"say": "Follow for more.", "show": "follow for more", "claim": False}]}
    p = tmp / "news.json"
    p.write_text(json.dumps(spec))
    return p


def test_news_builds_with_sources(tmp_path):
    res = news.build(_news_spec(tmp_path), tmp_path / "out", voice_override={"engines": ["silent"]})
    v = probe(Path(res["video"]))
    assert (v.width, v.height) == (1080, 1920)
    up = json.loads(Path(res["upload"]).read_text())
    assert "https://example.com/a" in up["description"] and "Streamer0" in up["description"]


def test_news_refuses_uncited_fact(tmp_path):
    with pytest.raises(meta.RuleError, match="cites no source"):
        news.build(_news_spec(tmp_path, cite=False), tmp_path / "out", voice_override={"engines": ["silent"]})


def test_tags_fit_youtube_limit():
    t = meta.tags([f"tag number {i}" for i in range(200)])
    assert sum(len(x) + 3 for x in t) <= 500


def test_mask_pixelates_the_box(tmp_path):
    from clipstudio.formats import compose as C
    clip = _split_clip(tmp_path / "m.mp4")
    lay = C.Layout.for_source(clip, "split")
    a = C.frame_png(clip, 1.0, tmp_path / "a.png", lay)
    b = C.frame_png(clip, 1.0, tmp_path / "b.png", C.Layout.for_source(clip, "split").with_mask([[0, 960, 540, 300]]))
    from PIL import Image, ImageChops
    diff = ImageChops.difference(Image.open(a).convert("RGB"), Image.open(b).convert("RGB"))
    assert diff.crop((0, 0, 540, 300)).getbbox() is not None      # masked area changed
    assert diff.crop((600, 350, 1080, 608)).getbbox() is None     # the rest did not
