import datetime
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable

from boomer.sound_player import SoundPlayer

logger = logging.getLogger(__name__)

_DAYS = {
    "lun": 0, "mar": 1, "mer": 2, "jeu": 3, "ven": 4, "sam": 5, "dim": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}
_DAY_NAMES = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]


def parse_days(spec: str) -> list[int] | None:
    spec = spec.lower().strip()
    if spec in ("tous", "all", "daily", "quotidien"):
        return None
    if spec in ("lun-ven", "mon-fri", "semaine", "weekdays"):
        return [0, 1, 2, 3, 4]
    if spec in ("sam-dim", "sat-sun", "weekend"):
        return [5, 6]
    days = [_DAYS[p.strip()[:3]] for p in spec.split(",") if p.strip()[:3] in _DAYS]
    return days if days else None


def days_label(days: list[int] | None) -> str:
    if days is None:
        return "tous les jours"
    return ", ".join(_DAY_NAMES[d] for d in days)


class Scheduler:
    def __init__(self, player: SoundPlayer, config_path: str = "schedules.json"):
        self._player = player
        self._config_path = config_path
        self._timers: dict[str, threading.Timer] = {}
        self._on_fire: Callable[[str, str], None] | None = None
        self._schedules: dict[str, dict] = self._load()

    def set_on_fire_callback(self, cb: Callable[[str, str], None]):
        self._on_fire = cb

    def _load(self) -> dict:
        if not os.path.exists(self._config_path):
            return {}
        with open(self._config_path) as f:
            return json.load(f)

    def _save(self):
        with open(self._config_path, "w") as f:
            json.dump(self._schedules, f, indent=2)

    def _next_delay(self, hour: int, minute: int, days: list[int] | None) -> float:
        now = datetime.datetime.now()
        for offset in range(8):
            candidate = (now + datetime.timedelta(days=offset)).replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= now:
                continue
            if days is None or candidate.weekday() in days:
                return (candidate - now).total_seconds()
        return 86400.0

    def _arm(self, schedule_id: str):
        sched = self._schedules.get(schedule_id)
        if not sched:
            return

        def fire():
            logger.info("Scheduled sound: %s", sched["sound"])
            self._player.play(sched["sound"])
            if self._on_fire:
                self._on_fire(schedule_id, sched["sound"])
            self._arm(schedule_id)

        delay = self._next_delay(sched["hour"], sched["minute"], sched.get("days"))
        logger.info("Schedule %s ('%s' at %s) armed, firing in %.0f min",
                    schedule_id, sched["sound"], sched["time"], delay / 60)
        timer = threading.Timer(delay, fire)
        timer.daemon = True
        self._timers[schedule_id] = timer
        timer.start()

    def start(self):
        for sid in self._schedules:
            self._arm(sid)

    def stop(self):
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()

    def add(self, time_str: str, sound: str, days: list[int] | None = None) -> str | None:
        try:
            h, m = map(int, time_str.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            return None
        sid = uuid.uuid4().hex[:6]
        self._schedules[sid] = {"time": time_str, "hour": h, "minute": m,
                                "sound": sound, "days": days}
        self._save()
        self._arm(sid)
        return sid

    def remove(self, schedule_id: str) -> bool:
        if schedule_id not in self._schedules:
            return False
        t = self._timers.pop(schedule_id, None)
        if t:
            t.cancel()
        del self._schedules[schedule_id]
        self._save()
        return True

    def list_all(self) -> list[dict]:
        return [{"id": sid, **sched} for sid, sched in self._schedules.items()]
