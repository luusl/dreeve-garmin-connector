"""Persistent record of which Garmin activities have already been handled.

Dreeve deletes files from its watch folder as soon as it imports them, so "is the file still
there?" can never answer "did we already fetch this?". This ledger is the only thing that can,
which makes it the one piece of state whose loss costs a full re-download.

Two properties follow from that: writes are atomic (a half-written ledger is worse than none),
and they are batched — the file is rewritten once per cycle, not once per activity.
"""

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

LEDGER_VERSION = 1
LEDGER_FILENAME = "ledger.json"
TEMP_SUFFIX = ".tmp"

logger = logging.getLogger(__name__)


class CorruptLedger(Exception):
    """Raised rather than silently starting over: a fresh ledger re-downloads everything since SINCE."""


class ActivityStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    SKIPPED = "skipped"
    FAILED = "failed"
    NO_FILE = "no-file"

    @property
    def is_terminal(self) -> bool:
        """Terminal activities are never reconsidered, so re-listing a long history stays cheap."""
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset({ActivityStatus.DELIVERED, ActivityStatus.SKIPPED, ActivityStatus.NO_FILE})


@dataclass(frozen=True)
class LedgerEntry:
    """Frozen on purpose: every change has to go through the ledger, which is what keeps the dirty flag honest."""

    activity_id: str
    start_time_gmt: datetime
    activity_type: str
    status: ActivityStatus
    first_seen: datetime
    files: tuple[str, ...] = ()
    attempts: int = 0
    last_attempt: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "startTimeGmt": self.start_time_gmt.isoformat(),
            "activityType": self.activity_type,
            "files": list(self.files),
            "status": self.status.value,
            "attempts": self.attempts,
            "firstSeen": self.first_seen.isoformat(),
            "lastAttempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, activity_id: str, raw: Any) -> "LedgerEntry":
        if not isinstance(raw, dict):
            raise CorruptLedger(f"Activity '{activity_id}' should be an object, got {type(raw).__name__}.")
        try:
            return cls(
                activity_id=activity_id,
                start_time_gmt=_instant(raw["startTimeGmt"]),
                activity_type=str(raw["activityType"]),
                status=ActivityStatus(raw["status"]),
                first_seen=_instant(raw["firstSeen"]),
                files=tuple(raw.get("files") or ()),
                attempts=int(raw.get("attempts", 0)),
                last_attempt=_optional_instant(raw.get("lastAttempt")),
                error=raw.get("error"),
            )
        except (KeyError, TypeError, ValueError) as exception:
            raise CorruptLedger(f"Activity '{activity_id}' is unreadable: {exception}.") from exception


class Ledger:
    def __init__(
        self,
        path: Path,
        entries: dict[str, LedgerEntry] | None = None,
        resolved_since: datetime | None = None,
        last_successful_sync: datetime | None = None,
    ) -> None:
        self._path = path
        self._entries = entries if entries is not None else {}
        self._resolved_since = resolved_since
        self._last_successful_sync = last_successful_sync
        self._dirty = False

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        cls._discard_interrupted_write(path)

        if not path.exists():
            logger.info("No ledger at %s yet, starting with an empty one", path)
            return cls(path)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exception:
            raise CorruptLedger(
                f"{path} is not valid JSON: {exception}. Inspect it, or delete it to start over — "
                f"a fresh ledger re-downloads everything since SINCE."
            ) from exception
        except OSError as exception:
            raise CorruptLedger(f"{path} could not be read: {exception}.") from exception

        if not isinstance(raw, dict):
            raise CorruptLedger(f"{path} should contain an object, got {type(raw).__name__}.")

        version = raw.get("version")
        if version != LEDGER_VERSION:
            raise CorruptLedger(
                f"{path} is version {version!r}, this build reads version {LEDGER_VERSION}. "
                f"It was most likely written by a newer connector; upgrade rather than downgrade."
            )

        activities = raw.get("activities") if raw.get("activities") is not None else {}
        if not isinstance(activities, dict):
            raise CorruptLedger(f"{path} has an 'activities' key that is not an object.")

        try:
            return cls(
                path=path,
                entries={str(key): LedgerEntry.from_dict(str(key), value) for key, value in activities.items()},
                resolved_since=_optional_instant(raw.get("resolvedSince")),
                last_successful_sync=_optional_instant(raw.get("lastSuccessfulSync")),
            )
        except (TypeError, ValueError) as exception:
            raise CorruptLedger(f"{path} has an unreadable timestamp: {exception}.") from exception

    @staticmethod
    def _discard_interrupted_write(path: Path) -> None:
        """A leftover temp file means a previous flush died mid-write; its contents are partial by definition."""
        temp_path = path.with_name(path.name + TEMP_SUFFIX)
        if not temp_path.exists():
            return

        logger.warning("Discarding %s, left behind by an interrupted write", temp_path)
        temp_path.unlink(missing_ok=True)

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def resolved_since(self) -> datetime | None:
        return self._resolved_since

    def resolve_since(self, instant: datetime) -> datetime:
        """Resolves SINCE once and remembers it, so `SINCE=now` does not skip activities added while we were down."""
        if self._resolved_since is None:
            self._resolved_since = instant
            self._dirty = True

        return self._resolved_since

    @property
    def last_successful_sync(self) -> datetime | None:
        return self._last_successful_sync

    def mark_successful_sync(self, at: datetime) -> None:
        self._last_successful_sync = at
        self._dirty = True

    def __contains__(self, activity_id: str) -> bool:
        return activity_id in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, activity_id: str) -> LedgerEntry | None:
        return self._entries.get(activity_id)

    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries.values())

    def counts_by_status(self) -> dict[ActivityStatus, int]:
        counted = Counter(entry.status for entry in self._entries.values())
        return {status: counted.get(status, 0) for status in ActivityStatus}

    def mark_seen(self, activity_id: str, start_time_gmt: datetime, activity_type: str, at: datetime) -> LedgerEntry:
        """Registers an activity as pending. A known activity is left exactly as it is, including its status."""
        known = self._entries.get(activity_id)
        if known is not None:
            return known

        entry = LedgerEntry(
            activity_id=activity_id,
            start_time_gmt=start_time_gmt,
            activity_type=activity_type,
            status=ActivityStatus.PENDING,
            first_seen=at,
        )
        self._entries[activity_id] = entry
        self._dirty = True

        return entry

    def mark_delivered(self, activity_id: str, files: tuple[str, ...], at: datetime) -> LedgerEntry:
        return self._record_attempt(activity_id, ActivityStatus.DELIVERED, at, files=files)

    def mark_no_file(self, activity_id: str, at: datetime) -> LedgerEntry:
        """For manually entered activities: permanent, so they are never retried."""
        return self._record_attempt(activity_id, ActivityStatus.NO_FILE, at)

    def mark_failed(self, activity_id: str, error: str, at: datetime) -> LedgerEntry:
        return self._record_attempt(activity_id, ActivityStatus.FAILED, at, error=error)

    def mark_skipped(self, activity_id: str, at: datetime) -> LedgerEntry:
        """Below SINCE or excluded by ACTIVITY_TYPES. No download was attempted, so the attempt count stands still."""
        return self._update(activity_id, status=ActivityStatus.SKIPPED, last_attempt=at, error=None)

    def _record_attempt(
        self,
        activity_id: str,
        status: ActivityStatus,
        at: datetime,
        files: tuple[str, ...] = (),
        error: str | None = None,
    ) -> LedgerEntry:
        known = self._require(activity_id)

        return self._update(
            activity_id,
            status=status,
            files=files or known.files,
            attempts=known.attempts + 1,
            last_attempt=at,
            error=error,
        )

    def _update(self, activity_id: str, **changes: Any) -> LedgerEntry:
        entry = replace(self._require(activity_id), **changes)
        self._entries[activity_id] = entry
        self._dirty = True

        return entry

    def _require(self, activity_id: str) -> LedgerEntry:
        known = self._entries.get(activity_id)
        if known is None:
            raise KeyError(f"Activity '{activity_id}' was never seen; call mark_seen() first.")

        return known

    def flush(self) -> None:
        """Writes the whole ledger, or nothing at all. Called once per cycle."""
        if not self._dirty:
            return

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(self._path.name + TEMP_SUFFIX)

        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(self.to_dict(), indent=2) + "\n")
            handle.flush()
            # Without this the rename can land before the contents do, and a power cut leaves an empty ledger.
            os.fsync(handle.fileno())

        temp_path.replace(self._path)
        self._dirty = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": LEDGER_VERSION,
            "resolvedSince": self._resolved_since.isoformat() if self._resolved_since else None,
            "lastSuccessfulSync": self._last_successful_sync.isoformat() if self._last_successful_sync else None,
            "activities": {activity_id: self._entries[activity_id].to_dict() for activity_id in sorted(self._entries)},
        }


def _instant(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise TypeError(f"expected an ISO 8601 timestamp, got {type(raw).__name__}")

    return datetime.fromisoformat(raw)


def _optional_instant(raw: Any) -> datetime | None:
    return None if raw is None else _instant(raw)
