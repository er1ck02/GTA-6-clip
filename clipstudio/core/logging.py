"""Structured logging: human-readable console output plus JSON lines on disk."""
from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "data", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        prefix = {"WARNING": "warning: ", "ERROR": "error: ", "CRITICAL": "error: "}.get(record.levelname, "")
        return f"{prefix}{record.getMessage()}"


def setup_logging(verbose: bool = False) -> None:
    global _CONFIGURED
    root = logging.getLogger("clipstudio")
    if _CONFIGURED:
        return
    root.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stderr)
    h.setLevel(logging.DEBUG if verbose else logging.INFO)
    h.setFormatter(ConsoleFormatter())
    root.addHandler(h)
    root.propagate = False
    _CONFIGURED = True


def add_file_log(path: Path) -> logging.Handler:
    """Attach a JSON-lines log file (one per project). Returns the handler so callers can detach it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("clipstudio")
    for h in root.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path.resolve():
            return h
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)
    return fh


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"clipstudio.{name}")


def log_event(logger: logging.Logger, level: int, msg: str, **data) -> None:
    logger.log(level, msg, extra={"data": data})
