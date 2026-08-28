import calendar
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

    def _period_start(self, period: str) -> datetime.datetime | None:
        now = datetime.datetime.now()
        if period == "week":
            return (now - datetime.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        if period == "day":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return None

    def _window(self, period: str, previous: bool = False) -> tuple[float, float] | None:
        """(start, end) of the period, or of the one before it. None means no limit."""
        start = self._period_start(period)
        if start is None:
            return None
        now = datetime.datetime.now()
        if not previous:
            return (start.timestamp(), now.timestamp())
        if period == "day":
            earlier = start - datetime.timedelta(days=1)
        elif period == "week":
            earlier = start - datetime.timedelta(days=7)
        else:
            earlier = (start - datetime.timedelta(days=1)).replace(day=1)
        # Same elapsed time, so a half-finished week compares against half a week
        return (earlier.timestamp(), earlier.timestamp() + (now - start).total_seconds())

    def _filter(self, period: str, actor: str | None = None, previous: bool = False) -> list[list]:
        with self._lock:
            events = list(self._events)
        if previous and period == "all":
            return []
        window = self._window(period, previous)
        if window is not None:
            start, end = window
            events = [e for e in events if start <= e[0] < end]
        if actor is not None:
            events = [e for e in events if e[2] == actor]
        return events

    def total(self, period: str = "all", actor: str | None = None, previous: bool = False) -> int:
        return len(self._filter(period, actor, previous))

    def top_sounds(self, period: str = "all", actor: str | None = None,
                   limit: int | None = 10, previous: bool = False) -> list[tuple[str, int]]:
        return Counter(e[1] for e in self._filter(period, actor, previous)).most_common(limit)

    def top_actors(self, period: str = "all", limit: int | None = 10,
                   humans_only: bool = False, previous: bool = False) -> list[tuple[str, int]]:
        """Rank the triggers. The MIDI keyboard usually dwarfs everyone, hence humans_only."""
        actors = (e[2] for e in self._filter(period, previous=previous))
        if humans_only:
            actors = (a for a in actors if a not in PSEUDO_ACTORS)
        return Counter(actors).most_common(limit)

    def _histogram(self, events: list[list], slot, size: int) -> list[int]:
        counts = [0] * size
        for event in events:
            index = slot(datetime.datetime.fromtimestamp(event[0]))
            if 0 <= index < size:
                counts[index] += 1
        return counts

    def timeline(self, period: str, sound: str | None = None,
                 actor: str | None = None) -> list[int]:
        """Plays slot by slot: one slot per hour for a day, one per day for a week or a month."""
        events = self._filter(period, actor)
        if sound is not None:
            events = [e for e in events if e[1] == sound]
        if period == "day":
            return self._histogram(events, lambda d: d.hour, 24)
        start = self._period_start(period)
        if start is None:
            return []
        size = 7 if period == "week" else calendar.monthrange(start.year, start.month)[1]
        return self._histogram(events, lambda d: (d.date() - start.date()).days, size)

    def by_hour(self, period: str = "week", sound: str | None = None, actor: str | None = None,
                per_hour: int = 1) -> list[int]:
        """Plays split into the day's slots, whatever the day: per_hour slots per hour."""
        events = self._filter(period, actor)
        if sound is not None:
            events = [e for e in events if e[1] == sound]
        return self._histogram(
            events, lambda d: d.hour * per_hour + d.minute * per_hour // 60, 24 * per_hour
        )

    def first_play(self) -> float | None:
        with self._lock:
            return self._events[0][0] if self._events else None
