"""In-process event bus. Stages publish progress here; the UI server streams it (SSE).

Kept dependency-free and thread-safe so the pipeline never depends on the UI.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, Callable

Listener = Callable[[dict], None]

_lock = threading.Lock()
_listeners: list[Listener] = []
_history: deque[dict] = deque(maxlen=500)
_seq = itertools.count(1)
# set by the job runner so every event knows which project/clip/job it belongs to
context: ContextVar[dict] = ContextVar("clipstudio_event_context", default={})


def publish(kind: str, **data: Any) -> dict:
    ev = {"seq": next(_seq), "ts": time.time(), "kind": kind, **context.get(), **data}
    with _lock:
        _history.append(ev)
        ls = list(_listeners)
    for fn in ls:
        try:
            fn(ev)
        except Exception:  # a broken listener must never break the pipeline
            pass
    return ev


def subscribe(fn: Listener) -> Callable[[], None]:
    with _lock:
        _listeners.append(fn)

    def unsubscribe() -> None:
        with _lock:
            if fn in _listeners:
                _listeners.remove(fn)

    return unsubscribe


def history(after: int = 0) -> list[dict]:
    with _lock:
        return [e for e in _history if e["seq"] > after]
