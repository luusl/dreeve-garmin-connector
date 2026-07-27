"""Which stretch of calendar to ask Garmin about.

Pure date arithmetic, no clock of its own: every function is handed `now`, which is what makes the
sync cycle testable without waiting for one.

Two ideas carry the whole module. A cycle re-lists the last few days rather than only what is new,
because watches sync late and activities get edited after the fact. And a first run against years of
history is walked in pages rather than asked for in one breath, because that is how accounts get
rate-limited.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from dreeve_garmin_connector.config import InvalidConfiguration, Since

BACKFILL_CHUNK_DAYS = 30


@dataclass(frozen=True)
class Window:
    """An inclusive range of calendar days, which is what Garmin's activity listing understands."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"A window cannot end before it starts, got {self.start} to {self.end}.")

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def __str__(self) -> str:
        return f"{self.start.isoformat()}..{self.end.isoformat()}"


def sync_start(*, configured: Since | None, already_resolved: datetime | None, now: datetime) -> datetime:
    """The lower bound of everything this connector will ever look at.

    Whatever the first run resolved always wins. Re-resolving `SINCE=now` every boot would silently
    skip every activity recorded while the container was down.
    """
    if already_resolved is not None:
        return already_resolved

    if configured is None:
        raise InvalidConfiguration(
            [
                "SINCE is required on the first run: it decides how far back to reach. "
                "Use a date (2026-01-01), a relative offset (-30d) or 'now'."
            ]
        )

    return configured.resolve(now)


def incremental_window(
    *,
    since: datetime,
    last_synced_at: datetime | None,
    lookback_days: int,
    now: datetime,
) -> Window | None:
    """The range a cycle should list, or nothing at all when the start lies in the future."""
    start = _utc_date(since)
    end = _utc_date(now)

    if last_synced_at is not None:
        # Re-listing the last few days is what catches a watch that synced late, or a ride that was
        # edited after it was already imported. It never reaches back past SINCE.
        start = max(start, _utc_date(last_synced_at) - timedelta(days=lookback_days))

    if start > end:
        return None

    return Window(start, end)


def chunked(window: Window, chunk_days: int = BACKFILL_CHUNK_DAYS) -> tuple[Window, ...]:
    """Splits a window into contiguous pages. A window smaller than a page comes back untouched."""
    if chunk_days < 1:
        raise ValueError(f"A window has to span at least one day, got {chunk_days}.")

    windows = []
    start = window.start
    while start <= window.end:
        end = min(start + timedelta(days=chunk_days - 1), window.end)
        windows.append(Window(start, end))
        start = end + timedelta(days=1)

    return tuple(windows)


def _utc_date(instant: datetime) -> date:
    """Everything is compared in UTC; a SINCE given with an offset must not shift the calendar day."""
    return instant.astimezone(UTC).date()
