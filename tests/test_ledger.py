import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from dreeve_garmin_connector.ledger import (
    LEDGER_VERSION,
    ActivityStatus,
    CorruptLedger,
    Ledger,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITY_ID = "12345678901"
STARTED_AT = datetime(2026, 7, 25, 6, 13, 42, tzinfo=UTC)
NOW = datetime(2026, 7, 25, 10, 12, 3, tzinfo=UTC)
LATER = datetime(2026, 7, 25, 11, 12, 3, tzinfo=UTC)


def fixture_at(name: str, destination: Path) -> Path:
    path = destination / "ledger.json"
    shutil.copy(FIXTURES / name, path)

    return path


def test_it_starts_empty_when_there_is_no_ledger_yet(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.json")

    assert len(ledger) == 0
    assert ledger.resolved_since is None
    assert ledger.last_successful_sync is None
    assert ledger.is_dirty is False


def test_it_reads_an_existing_ledger(tmp_path: Path) -> None:
    ledger = Ledger.load(fixture_at("ledger.json", tmp_path))

    assert len(ledger) == 2
    assert ledger.resolved_since == datetime(2026, 1, 1, tzinfo=UTC)
    assert ledger.last_successful_sync == datetime(2026, 7, 25, 10, 12, tzinfo=UTC)

    delivered = ledger.entry(ACTIVITY_ID)
    assert delivered is not None
    assert delivered.status is ActivityStatus.DELIVERED
    assert delivered.start_time_gmt == STARTED_AT
    assert delivered.activity_type == "cycling"
    assert delivered.files == ("12345678901.fit",)
    assert delivered.attempts == 1
    assert delivered.error is None

    failed = ledger.entry("12345678902")
    assert failed is not None
    assert failed.status is ActivityStatus.FAILED
    assert failed.attempts == 3
    assert failed.error == "connection reset"


def test_it_survives_a_round_trip(tmp_path: Path) -> None:
    path = fixture_at("ledger.json", tmp_path)
    original = Ledger.load(path)
    original.mark_successful_sync(LATER)
    original.flush()

    assert Ledger.load(path).to_dict() == original.to_dict()


def test_a_delivered_activity_stays_delivered_once_dreeve_removed_the_file(tmp_path: Path) -> None:
    # The whole reason this module exists: the watch folder is emptied by Dreeve, so it can never
    # be asked whether an activity was already imported.
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    delivered_file = watch_dir / f"{ACTIVITY_ID}.fit"
    delivered_file.write_bytes(b"fit")

    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path)
    ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)
    ledger.mark_delivered(ACTIVITY_ID, (delivered_file.name,), NOW)
    ledger.flush()

    delivered_file.unlink()

    entry = Ledger.load(path).entry(ACTIVITY_ID)
    assert entry is not None
    assert entry.status is ActivityStatus.DELIVERED
    assert entry.status.is_terminal is True


def test_it_refuses_to_read_a_truncated_ledger(tmp_path: Path) -> None:
    path = fixture_at("ledger-truncated.json", tmp_path)

    with pytest.raises(CorruptLedger) as raised:
        Ledger.load(path)

    assert str(path) in str(raised.value)
    assert "not valid JSON" in str(raised.value)
    assert "re-downloads everything since SINCE" in str(raised.value)


def test_it_refuses_a_ledger_written_by_a_newer_build(tmp_path: Path) -> None:
    with pytest.raises(CorruptLedger) as raised:
        Ledger.load(fixture_at("ledger-newer-version.json", tmp_path))

    assert f"is version 2, this build reads version {LEDGER_VERSION}" in str(raised.value)


def test_it_names_the_activity_it_could_not_read(tmp_path: Path) -> None:
    with pytest.raises(CorruptLedger) as raised:
        Ledger.load(fixture_at("ledger-broken-entry.json", tmp_path))

    assert f"Activity '{ACTIVITY_ID}' is unreadable" in str(raised.value)


def test_it_rejects_a_timestamp_that_is_not_a_timestamp(tmp_path: Path) -> None:
    with pytest.raises(CorruptLedger) as raised:
        Ledger.load(fixture_at("ledger-broken-timestamp.json", tmp_path))

    assert "has an unreadable timestamp" in str(raised.value)
    assert "expected an ISO 8601 timestamp, got int" in str(raised.value)


def test_it_reports_a_ledger_it_cannot_read_at_all(tmp_path: Path) -> None:
    # A directory where the ledger should be: the container was started with the wrong volume mapping.
    path = tmp_path / "ledger.json"
    path.mkdir()

    with pytest.raises(CorruptLedger) as raised:
        Ledger.load(path)

    assert "could not be read" in str(raised.value)


@pytest.mark.parametrize(
    ("contents", "expected_message"),
    [
        ("[]", "should contain an object, got list"),
        ('{"version": 1, "activities": []}', "'activities' key that is not an object"),
        ('{"version": 1, "activities": {"1": "delivered"}}', "should be an object, got str"),
    ],
)
def test_it_rejects_a_ledger_with_the_wrong_shape(contents: str, expected_message: str, tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(contents, encoding="utf-8")

    with pytest.raises(CorruptLedger) as raised:
        Ledger.load(path)

    assert expected_message in str(raised.value)


def test_it_discards_a_temp_file_left_behind_by_an_interrupted_write(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = fixture_at("ledger.json", tmp_path)
    leftover = tmp_path / "ledger.json.tmp"
    leftover.write_text('{"version": 1, "activities": {"999": ', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        ledger = Ledger.load(path)

    assert leftover.exists() is False
    assert len(ledger) == 2
    assert "left behind by an interrupted write" in caplog.text


def test_a_new_ledger_only_becomes_visible_once_it_is_written_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path)
    ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)
    ledger.flush()

    observed: dict[str, Any] = {}
    real_replace = Path.replace

    def spy(source: Path, destination: Any) -> Path:
        # At the moment of the rename both files must be complete and parseable.
        observed["temp"] = json.loads(source.read_text(encoding="utf-8"))
        observed["destination"] = json.loads(Path(destination).read_text(encoding="utf-8"))

        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", spy)

    ledger.mark_seen("22222222222", STARTED_AT, "running", NOW)
    ledger.flush()

    assert "22222222222" in observed["temp"]["activities"]
    assert "22222222222" not in observed["destination"]["activities"]
    assert list(tmp_path.glob("*.tmp")) == []


def test_it_writes_nothing_while_it_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = fixture_at("ledger.json", tmp_path)
    ledger = Ledger.load(path)

    def refuse(_source: Path, _destination: Any) -> Path:
        raise AssertionError("a clean ledger must not be rewritten")

    monkeypatch.setattr(Path, "replace", refuse)

    ledger.flush()

    assert ledger.is_dirty is False


def test_it_creates_the_state_directory_on_the_first_flush(tmp_path: Path) -> None:
    path = tmp_path / "state" / "ledger.json"
    ledger = Ledger.load(path)
    ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)

    ledger.flush()

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == LEDGER_VERSION


def test_it_registers_an_unknown_activity_as_pending(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.json")

    entry = ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)

    assert entry.status is ActivityStatus.PENDING
    assert entry.status.is_terminal is False
    assert entry.first_seen == NOW
    assert entry.attempts == 0
    assert ACTIVITY_ID in ledger
    assert ledger.is_dirty is True


def test_seeing_a_known_activity_again_changes_nothing(tmp_path: Path) -> None:
    path = fixture_at("ledger.json", tmp_path)
    ledger = Ledger.load(path)

    entry = ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "swimming", LATER)

    assert entry.status is ActivityStatus.DELIVERED
    assert entry.activity_type == "cycling"
    assert entry.first_seen == datetime(2026, 7, 25, 10, 12, 3, tzinfo=UTC)
    assert ledger.is_dirty is False


@pytest.mark.parametrize(
    ("mark", "expected_status", "expected_attempts"),
    [
        ("delivered", ActivityStatus.DELIVERED, 1),
        ("no_file", ActivityStatus.NO_FILE, 1),
        ("failed", ActivityStatus.FAILED, 1),
        ("skipped", ActivityStatus.SKIPPED, 0),
    ],
)
def test_only_a_real_download_attempt_is_counted(
    mark: str, expected_status: ActivityStatus, expected_attempts: int, tmp_path: Path
) -> None:
    ledger = Ledger.load(tmp_path / "ledger.json")
    ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)

    marks = {
        "delivered": lambda: ledger.mark_delivered(ACTIVITY_ID, (f"{ACTIVITY_ID}.fit",), LATER),
        "no_file": lambda: ledger.mark_no_file(ACTIVITY_ID, LATER),
        "failed": lambda: ledger.mark_failed(ACTIVITY_ID, "boom", LATER),
        "skipped": lambda: ledger.mark_skipped(ACTIVITY_ID, LATER),
    }
    entry = marks[mark]()

    assert entry.status is expected_status
    assert entry.status.is_terminal is (expected_status is not ActivityStatus.FAILED)
    assert entry.attempts == expected_attempts
    assert entry.last_attempt == LATER


def test_it_accumulates_attempts_and_keeps_the_last_error(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.json")
    ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)

    ledger.mark_failed(ACTIVITY_ID, "connection reset", NOW)
    entry = ledger.mark_failed(ACTIVITY_ID, "connection reset again", LATER)

    assert entry.attempts == 2
    assert entry.error == "connection reset again"


def test_delivery_clears_an_earlier_error(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.json")
    ledger.mark_seen(ACTIVITY_ID, STARTED_AT, "cycling", NOW)
    ledger.mark_failed(ACTIVITY_ID, "connection reset", NOW)

    entry = ledger.mark_delivered(ACTIVITY_ID, (f"{ACTIVITY_ID}.fit",), LATER)

    assert entry.error is None
    assert entry.files == (f"{ACTIVITY_ID}.fit",)


def test_it_refuses_to_mark_an_activity_it_never_saw(tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.json")

    with pytest.raises(KeyError, match="was never seen"):
        ledger.mark_delivered(ACTIVITY_ID, (), NOW)


def test_it_resolves_since_once_and_then_stands_by_it(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = Ledger.load(path)

    assert ledger.resolve_since(NOW) == NOW
    assert ledger.resolve_since(LATER) == NOW

    ledger.flush()
    assert Ledger.load(path).resolved_since == NOW


def test_it_counts_every_status_including_the_empty_ones(tmp_path: Path) -> None:
    ledger = Ledger.load(fixture_at("ledger.json", tmp_path))

    assert ledger.counts_by_status() == {
        ActivityStatus.PENDING: 0,
        ActivityStatus.DELIVERED: 1,
        ActivityStatus.SKIPPED: 0,
        ActivityStatus.FAILED: 1,
        ActivityStatus.NO_FILE: 0,
    }
