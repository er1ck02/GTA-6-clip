from clipstudio.live.buffer import reconnect_flags


def test_twitch_playlists_get_no_reconnect_flags():
    # -reconnect_streamed makes FFmpeg retry the end of every Twitch HLS segment forever: nothing is recorded
    assert reconnect_flags("https://usw23.playlist.ttvnw.net/v1/playlist/abc.m3u8") == []


def test_other_streams_keep_reconnect_flags():
    flags = reconnect_flags("https://manifest.googlevideo.com/api/manifest/hls_playlist/x/index.m3u8")
    assert "-reconnect_streamed" in flags and "-reconnect" in flags
