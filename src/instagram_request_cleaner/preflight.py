"""Small fail-closed helpers used by tests and optional schedulers."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def allows_service_start(exit_code: int) -> bool:
    return exit_code == 0


def deadline_is_future(value: str, now: datetime | None = None) -> bool:
    try:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            return False
        if value == "next-istanbul-0800":
            local = current.astimezone(ZoneInfo("Europe/Istanbul"))
            deadline = local.replace(hour=8, minute=0, second=0, microsecond=0)
            if deadline <= local:
                from datetime import timedelta
                deadline += timedelta(days=1)
            return deadline.astimezone(timezone.utc) > current.astimezone(timezone.utc)
        deadline = datetime.fromisoformat(value)
        return bool(deadline.tzinfo and deadline.astimezone(timezone.utc) > current.astimezone(timezone.utc))
    except (TypeError, ValueError):
        return False
