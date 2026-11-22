"""Audio graph, punch-zoom and enhancement filter construction (no encoding)."""
from clipstudio.config import load_config
from clipstudio.rendering.render import build_audio_graph, gameplay_fx, punch_times, zoom_expr


def _acfg(*ov):
    return load_config(overrides=list(ov)).audio


def test_single_track_gets_voice_chain():
    g = build_audio_graph(1, None, _acfg())
    assert g.startswith("[0:a:0]highpass") and g.endswith("[amix]")
    assert build_audio_graph(1, None, _acfg("audio.voice_enhancement=off")) == "[0:a:0]anull[amix]"


def test_multi_track_without_voice_mixes_everything():
    g = build_audio_graph(3, None, _acfg())
    assert "[0:a:0][0:a:1][0:a:2]amix=inputs=3" in g and "sidechaincompress" not in g


def test_voice_track_ducks_game():
    g = build_audio_graph(2, 1, _acfg("audio.duck_ratio=6"))
    assert g.startswith("[0:a:1]highpass")
    assert "[0:a:0]anull[game]" in g
    assert "[game][key]sidechaincompress" in g and "ratio=6" in g
    assert g.endswith("[amix]")


def test_voice_track_without_ducking():
    g = build_audio_graph(3, 0, _acfg("audio.duck_game=false", "audio.voice_enhancement=strong"))
    assert "sidechaincompress" not in g and "afftdn" in g
    assert "[0:a:1][0:a:2]amix=inputs=2" in g


def test_out_of_range_voice_falls_back_to_mix():
    assert "sidechaincompress" not in build_audio_graph(2, 5, _acfg())


def test_punch_times_filters_and_spaces():
    assert punch_times([5.0, 5.5, 0.05, 9.9, 7.0, 2.0], 10.0, 3) == [2.0, 5.0, 7.0]
    assert punch_times([5.0, 7.0], 10.0, 1) == [5.0]
    assert punch_times([], 10.0, 3) == []


def test_zoom_expr_values():
    e = zoom_expr([2.0], 1.2)

    def at(t):
        # evaluate the ffmpeg expression with Python semantics for clip()
        return eval(e.replace("clip(", "_clip("), {"t": t, "_clip": lambda x, lo, hi: max(lo, min(hi, x))})

    assert at(0.0) == 1.0 and at(5.0) == 1.0
    assert abs(at(2.0) - 1.2) < 1e-6 and abs(at(2.4) - 1.2) < 1e-6
    assert 1.0 < at(2.7) < 1.2


def test_gameplay_fx():
    ecfg = load_config().enhance
    fx = gameplay_fx(1080, 1920, ecfg, [3.0])
    assert "eval=frame" in fx and "crop=1080:1920" in fx and "cas=" in fx and "eq=contrast" in fx
    assert "eval=frame" not in gameplay_fx(1080, 1920, ecfg, [])
    off = load_config(overrides=["enhance.sharpen=0", "enhance.vibrance=0", "enhance.contrast=0"]).enhance
    assert gameplay_fx(1080, 1920, off, None) == ""
    assert gameplay_fx(1080, 1920, None, [3.0]) == ""


def test_cuts_helpers():
    from clipstudio.core.models import Word, cut_time, normalize_cuts, remap_words

    assert normalize_cuts([(12, 14), (13, 15), (9, 10.5), (19.9, 30), (16, 16.1)], 10, 20) == [(10.0, 10.5), (12.0, 15.0)]
    cuts = [(12.0, 15.0)]
    assert cut_time(11, cuts) == 11 and cut_time(13, cuts) == 12 and cut_time(16, cuts) == 13
    ws = [Word(text="a", start=11.0, end=11.5), Word(text="gone", start=13.0, end=13.4), Word(text="b", start=15.5, end=16.0)]
    out = remap_words(ws, cuts)
    assert [w.text for w in out] == ["a", "b"] and (out[1].start, out[1].end) == (12.5, 13.0)


def test_cut_graphs():
    from clipstudio.rendering.render import keep_expr

    assert keep_expr([(1.0, 2.0), (3.0, 4.5)]) == "'not(between(t,1.000,1.999)+between(t,3.000,4.499))'"
    g = build_audio_graph(1, None, _acfg(), [(1.0, 2.0)])
    assert g.endswith("aselect='not(between(t,1.000,1.999))',asetpts=N/SR/TB[amix]") and "[amix0]" in g
