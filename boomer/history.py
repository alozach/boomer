import json
import logging
import os
import threading
import time
from collections import deque

logger = logging.getLogger(__name__)

_FLUSH_DELAY = 10.0
_MAX_ENTRIES = 1000


class History:
    """Who did what, as [timestamp, actor, text] triples; actor is a Slack user ID or a pseudo-actor."""

    def __init__(self, path: str = "history.json"):
        self._path = path
        self._lock = threading.Lock()
        self._entries: deque[list] = deque(self._load(), maxlen=_MAX_ENTRIES)
        self._flush_timer: threading.Timer | None = None

    def _load(self) -> list[list]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path) as f:
                return json.load(f).get("entries", [])
        except (json.JSONDecodeError, OSError):
            logger.exception("Cannot read %s, starting from an empty history", self._path)
            return []

    def record(self, actor: str, text: str):
        with self._lock:
            self._entries.append([int(time.time()), actor, text])
            if self._flush_timer is None:
                self._flush_timer = threading.Timer(_FLUSH_DELAY, self.flush)
                self._flush_timer.daemon = True
                self._flush_timer.start()

    def flush(self):
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            entries = list(self._entries)
        try:
            with open(self._path, "w") as f:
                json.dump({"entries": entries}, f)
        except OSError:
            logger.exception("Cannot write %s", self._path)

    def last(self, limit: int, actor: str | None = None) -> list[list]:
        """Most recent first."""
        with self._lock:
            entries = list(self._entries)
        if actor is not None:
            entries = [e for e in entries if e[1] == actor]
        return entries[::-1][:limit]
