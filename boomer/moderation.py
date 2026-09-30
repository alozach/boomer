import logging
import os

logger = logging.getLogger(__name__)


class Moderation:
    """Admins come from the BOOMER_ADMINS env var."""

    def __init__(self):
        raw = os.getenv("BOOMER_ADMINS", "")
        self.admins = frozenset(u.strip() for u in raw.split(",") if u.strip())
        if self.admins:
            logger.info("Boomer admins: %s", ", ".join(sorted(self.admins)))
        else:
            logger.warning("BOOMER_ADMINS is not set: nobody can read the history")

    def is_admin(self, user_id: str | None) -> bool:
        return user_id in self.admins
