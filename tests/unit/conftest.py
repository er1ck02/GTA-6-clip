import pytest


@pytest.fixture(autouse=True)
def _no_env_overrides(monkeypatch):
    for k in ("CLIPSTUDIO_WHISPER_MODEL", "CLIPSTUDIO_DEVICE", "CLIPSTUDIO_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
