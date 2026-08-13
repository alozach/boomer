import datetime
import json
import logging
import os
import threading
from collections import Counter

logger = logging.getLogger(__name__)

# Pseudo-users for plays that nobody triggered from Slack
ACTOR_MIDI = "midi"
ACTOR_SCHEDULE = "schedule"
PSEUDO_ACTORS = (ACTOR_MIDI, ACTOR_SCHEDULE)

_FLUSH_DELAY = 10.0
_MAX_EVENTS = 50000


class Stats:
    """Play history, persisted as [timestamp, sound, actor] triples."""

    def __init__(self, path: str = "stats.json"):
        self._path = path
        self._lock = threading.Lock()
        self._events: list[list] = self._load()
        self._flush_timer: threading.Timer | None = None

    def _load(self) -> list[list]:
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path) as f:
                return json.load(f).get("events", [])
        except (json.JSONDecodeError, OSError):
            logger.exception("Cannot read %s, starting from an empty history", self._path)
            return []

    def record(self, sound: str, actor: str):
        with self._lock:
            self._events.append([int(datetime.datetime.now().timestamp()), sound, actor])
            if len(self._events) > _MAX_EVENTS:
                del self._events[: len(self._events) - _MAX_EVENTS]
            self._schedule_flush()

    def _schedule_flush(self):
        """Batch writes: the SD card of a Pi does not need one rewrite per played sound."""
        if self._flush_timer is not None:
            return
        self._flush_timer = threading.Timer(_FLUSH_DELAY, self.flush)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def flush(self):
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            events = list(self._events)
        try:
            with open(self._path, "w") as f:
                json.dump({"events": events}, f)
        except OSError:
            logger.exception("Cannot write %s", self._path)

    def _since(self, period: str) -> float | None:
        now = datetime.datetime.now()
        if period == "week":
            start = (now - datetime.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        elif period == "day":
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif period == "month":
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            return None
        return start.timestamp()

    def _filter(self, period: str, actor: str | None = None) -> list[list]:
        since = self._since(period)
        with self._lock:
            events = list(self._events)
        if since is not None:
            events = [e for e in events if e[0] >= since]
        if actor is not None:
            events = [e for e in events if e[2] == actor]
        return events

    def total(self, period: str = "all", actor: str | None = None) -> int:
        return len(self._filter(period, actor))

    def top_sounds(self, period: str = "all", actor: str | None = None,
                   limit: int | None = 10) -> list[tuple[str, int]]:
        return Counter(e[1] for e in self._filter(period, actor)).most_common(limit)

    def top_actors(self, period: str = "all", limit: int | None = 10,
                   humans_only: bool = False) -> list[tuple[str, int]]:
        """Rank the triggers. The MIDI keyboard usually dwarfs everyone, hence humans_only."""
        actors = (e[2] for e in self._filter(period))
        if humans_only:
            actors = (a for a in actors if a not in PSEUDO_ACTORS)
        return Counter(actors).most_common(limit)

    def signature_sound(self, actor: str, period: str = "all") -> str | None:
        """The sound this actor played the most."""
        top = self.top_sounds(period, actor, limit=1)
        return top[0][0] if top else None

    def first_play(self) -> float | None:
        with self._lock:
            return self._events[0][0] if self._events else None
