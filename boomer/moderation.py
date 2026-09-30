import logging
import os
import re
import itertools
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

DEFAULT_BAN_SECONDS = 10 * 60
# A banned user hammering a button is shamed once, not once per click
_SHAME_COOLDOWN = 30
# Every third attempt during a ban adds this much to it
ATTEMPTS_BEFORE_PENALTY = 3
PENALTY_SECONDS = 10 * 60

_DURATION_RE = re.compile(r"(\d+)\s*(heures?|h|minutes?|mins?|mn|m|secondes?|sec|s)?")
_UNIT_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(text: str) -> int | None:
    """`10`, `10m`, `1h30`, `90s`… in seconds. A bare number counts minutes."""
    text = text.strip().lower().replace(" ", "")
    if not text:
        return None
    total, pos = 0, 0
    last_unit = None
    for match in _DURATION_RE.finditer(text):
        if match.start() != pos:
            return None
        pos = match.end()
        unit = (match.group(2) or "")[:1]
        if not unit:
            # `1h30`: a trailing bare number follows the previous unit down one step
            unit = {"h": "m", "m": "s"}.get(last_unit, "m")
        total += int(match.group(1)) * _UNIT_SECONDS[unit]
        last_unit = unit
    return total if pos == len(text) and total > 0 else None


def format_duration(seconds: float) -> str:
    seconds = max(1, round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d}" if minutes else f"{hours} h"
    if minutes:
        return f"{minutes} min" if not secs or minutes >= 10 else f"{minutes} min {secs:02d}"
    return f"{secs} s"


@dataclass
class BanRequest:
    id: str
    requester: str
    target: str
    seconds: int
    reason: str


class Moderation:
    """Admins come from the BOOMER_ADMINS env var; bans and requests live in memory and end with a restart."""

    def __init__(self):
        raw = os.getenv("BOOMER_ADMINS", "")
        self.admins = frozenset(u.strip() for u in raw.split(",") if u.strip())
        self._bans: dict[str, float] = {}
        self._requests: dict[str, BanRequest] = {}
        self._request_ids = itertools.count(1)
        self._last_shame: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()
        if self.admins:
            logger.info("Boomer admins: %s", ", ".join(sorted(self.admins)))
        else:
            logger.warning("BOOMER_ADMINS is not set: nobody can ban, read the history "
                           "or handle ban requests")

    def is_admin(self, user_id: str | None) -> bool:
        return user_id in self.admins

    def ban(self, user_id: str, seconds: int) -> float:
        until = time.time() + seconds
        with self._lock:
            self._bans[user_id] = until
            self._attempts.pop(user_id, None)
        return until

    def unban(self, user_id: str) -> bool:
        with self._lock:
            self._attempts.pop(user_id, None)
            return self._bans.pop(user_id, None) is not None

    def record_attempt(self, user_id: str) -> bool:
        """Count an interaction during a ban; True when it earned the ban an extension."""
        with self._lock:
            if user_id not in self._bans:
                return False
            count = self._attempts.get(user_id, 0) + 1
            if count < ATTEMPTS_BEFORE_PENALTY:
                self._attempts[user_id] = count
                return False
            self._attempts[user_id] = 0
            self._bans[user_id] += PENALTY_SECONDS
            return True

    def ban_remaining(self, user_id: str | None) -> float:
        """Seconds left on the user's ban, 0 when free to play."""
        if not user_id:
            return 0
        with self._lock:
            until = self._bans.get(user_id)
            if until is None:
                return 0
            left = until - time.time()
            if left <= 0:
                del self._bans[user_id]
                return 0
            return left

    def active_bans(self) -> list[tuple[str, float]]:
        now = time.time()
        with self._lock:
            return sorted(((u, until - now) for u, until in self._bans.items() if until > now),
                          key=lambda ban: ban[1])

    def shame_due(self, user_id: str) -> bool:
        now = time.time()
        with self._lock:
            if now - self._last_shame.get(user_id, 0) < _SHAME_COOLDOWN:
                return False
            self._last_shame[user_id] = now
            return True

    def add_request(self, requester: str, target: str, seconds: int, reason: str) -> BanRequest | None:
        """None when the requester already has a request waiting, or the target is already accused."""
        with self._lock:
            if any(r.requester == requester or r.target == target for r in self._requests.values()):
                return None
            request = BanRequest(str(next(self._request_ids)), requester, target, seconds, reason)
            self._requests[request.id] = request
            return request

    def pending_request(self, requester: str, target: str) -> BanRequest | None:
        with self._lock:
            return next((r for r in self._requests.values()
                         if r.requester == requester or r.target == target), None)

    def take_request(self, request_id: str) -> BanRequest | None:
        """Hand the request over to exactly one admin, whoever clicks first."""
        with self._lock:
            return self._requests.pop(request_id, None)
