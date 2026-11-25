"""Twitch emote ids captured from chat tags and used by the chat overlay (no network)."""
import json

from clipstudio.chat.emotes import EmoteIndex
from clipstudio.config import load_config
from clipstudio.live.chat import irc_emotes, parse_irc_line
from clipstudio.live.session import LiveSession, Moment

LINE = ("@badge-info=subscriber/12;color=#FF0000;display-name=Viewer;emotes=300354391:0-3,10-13/25:5-9;"
        "id=abc;mod=0 :viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #xqc :xqcL Kappa xqcL")


def test_tagged_privmsg_gives_text_and_emote_ids():
    user, text = parse_irc_line(LINE)
    assert (user, text) == ("viewer", "xqcL Kappa xqcL")
    assert irc_emotes(LINE, text) == {"xqcL": "300354391", "Kappa": "25"}
    assert irc_emotes(":v!v@v PRIVMSG #xqc :hi", "hi") == {}
    assert irc_emotes("@emotes=;id=1 :v!v@v PRIVMSG #xqc :hi", "hi") == {}


def test_session_saves_emote_ids_and_overlay_index_uses_them(tmp_path):
    cfg = load_config(overrides=[f"paths.data_dir={tmp_path}"])
    out = tmp_path / "live" / "xqc_20261122_050000"
    s = LiveSession(cfg, None, None, out, channel="xqc")
    s.emote_ids = {"xqcL": "300354391"}
    video = out / "moments" / "m001_10s.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    s.chat_log = [(5.0, "viewer", "xqcL")]
    s._write_chat(Moment("m001_10s", {}, (0, 10), saved=(0, 10)), video)
    assert json.loads((out / "emotes.json").read_text()) == {"xqcL": "300354391"}
    idx = EmoteIndex.from_source(tmp_path / "cache", video, providers=())
    assert idx.channel == "xqc" and idx.is_emote("xqcL")
    assert idx.urls["xqcL"][1].endswith("/300354391/default/dark/2.0")
