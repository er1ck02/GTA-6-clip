"""Exception hierarchy. Every failure the user can act on gets a clear message."""


class ClipStudioError(Exception):
    """Base class for expected, user-facing errors."""


class ConfigError(ClipStudioError):
    pass


class DependencyMissing(ClipStudioError):
    pass


class MediaError(ClipStudioError):
    """The source or an output file is unreadable, invalid or corrupt."""


class StageFailed(ClipStudioError):
    def __init__(self, stage: str, message: str):
        super().__init__(f"stage '{stage}' failed: {message}")
        self.stage = stage


class Cancelled(ClipStudioError):
    pass
