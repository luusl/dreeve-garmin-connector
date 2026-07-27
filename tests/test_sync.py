import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dreeve_garmin_connector.auth import NO_SESSION_INSTRUCTION, AuthenticationRequired
from dreeve_garmin_connector.cli import EXIT_FAILED, EXIT_OK, main
from dreeve_garmin_connector.config import Config, FallbackFormat
from dreeve_garmin_connector.delivery import UndeliverableFile, WatchFolder
from dreeve_garmin_connector.garmin import (
    Activity,
    ActivityNotFound,
    AuthenticationFailed,
    ConnectionFailed,
    RateLimited,
)
from dreeve_garmin_connector.ledger import LEDGER_FILENAME, ActivityStatus, Ledger
from dreeve_garmin_connector.logging_ import Secrets
from dreeve_garmin_connector.sync import Sync, SystemClock
from tests.stubs import FakeClock, FakeGarminClient, StubGarminSession

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = json.loads((FIXTURES / "garmin-activities.json").read_text(encoding="utf-8"))
ARCHIVE = (FIXTURES / "activity-single-fit.zip").read_bytes()
ARCHIVE_WITHOUT_FIT = (FIXTURES / "activity-no-fit.zip").read_bytes()
MALFORMED_ARCHIVE = (FIXTURES / "activity-malformed.zip").read_bytes()

NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
RIDE = Activity("111", datetime(2026, 7, 24, 6, 0, tzinfo=UTC), "cycling", "Morning Ride")
RUN = Activity("222", datetime(2026, 7, 24, 18, 0, tzinfo=UTC), "running", "Evening Run")
SWIM = Activity("333", datetime(2026, 7, 25, 7, 0, tzinfo=UTC), "lap_swimming", "Swim")


@dataclass
class Harness:
    sync: Sync
    sync_config: Config
    ledger: Ledger
    watch_dir: Path
    watch_folder: WatchFolder
    state_dir: Path
    client: FakeGarminClient
    clock: FakeClock


def harness_for(tmp_path: Path, client: FakeGarminClient, **env: str) -> Harness:
    config = Config.from_env(
        {
            "GARMIN_EMAIL": "rider@example.com",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "SINCE": "2026-07-01",
            "DOWNLOAD_DELAY_SECONDS": "0",
            **env,
        }
    )
    watch_folder = WatchFolder(config.watch_dir, config.on_conflict)
    watch_folder.prepare()
    ledger = Ledger.load(config.state_dir / LEDGER_FILENAME)
    clock = FakeClock(NOW)

    return Harness(
        sync=Sync(config, ledger, client, watch_folder, clock),
        sync_config=config,
        ledger=ledger,
        watch_dir=config.watch_dir,
        watch_folder=watch_folder,
        state_dir=config.state_dir,
        client=client,
        clock=clock,
    )


def test_it_delivers_a_new_activity(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}))

    result = harness.sync.run_once()

    assert (harness.watch_dir / "111.fit").read_bytes() == b".FIT single"
    assert result.delivered == 1
    assert result.listed == 1
    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.status is ActivityStatus.DELIVERED
    assert entry.files == ("111.fit",)


def test_it_records_the_cycle_and_writes_the_ledger_once(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}))

    harness.sync.run_once()

    stored = Ledger.load(harness.state_dir / LEDGER_FILENAME)
    assert stored.last_successful_sync == NOW
    assert stored.resolved_since == datetime(2026, 7, 1, tzinfo=UTC)
    assert stored.entry("111") is not None


def test_a_delivered_activity_is_never_downloaded_twice(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}))
    harness.sync.run_once()

    # Dreeve imports the file and deletes it; the folder can no longer answer "did we fetch this?".
    (harness.watch_dir / "111.fit").unlink()
    result = harness.sync.run_once()

    assert harness.client.downloaded == ["111"]
    assert result.delivered == 0


def test_an_activity_from_before_since_is_skipped_rather_than_downloaded(tmp_path: Path) -> None:
    # Listing works in whole days, so the first day of the window reaches back past SINCE itself.
    harness = harness_for(
        tmp_path,
        FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}),
        SINCE="2026-07-24T12:00:00+00:00",
    )

    result = harness.sync.run_once()

    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.status is ActivityStatus.SKIPPED
    assert entry.attempts == 0
    assert harness.client.downloaded == []
    assert result.delivered == 0


def test_it_downloads_no_more_than_the_cap_and_carries_the_rest_over(tmp_path: Path) -> None:
    client = FakeGarminClient(
        activities=(RIDE, RUN, SWIM),
        archives={"111": ARCHIVE, "222": ARCHIVE, "333": ARCHIVE},
    )
    harness = harness_for(tmp_path, client, MAX_DOWNLOADS_PER_CYCLE="2")

    first = harness.sync.run_once()

    # Oldest first, so the activities that have waited longest go first.
    assert client.downloaded == ["111", "222"]
    assert first.delivered == 2
    assert first.backlog == 1

    second = harness.sync.run_once()

    assert client.downloaded == ["111", "222", "333"]
    assert second.delivered == 1
    assert second.backlog == 0


def test_the_backlog_outlives_the_window_it_was_found_in(tmp_path: Path) -> None:
    # After the first successful cycle the window shrinks to the lookback, so anything older could
    # never be listed again. The ledger is what keeps it reachable.
    old = Activity("111", datetime(2026, 7, 2, 6, 0, tzinfo=UTC), "cycling", "Ride")
    recent = Activity("222", datetime(2026, 7, 24, 6, 0, tzinfo=UTC), "cycling", "Ride")
    client = FakeGarminClient(activities=(old, recent), archives={"111": ARCHIVE, "222": ARCHIVE})
    harness = harness_for(tmp_path, client, MAX_DOWNLOADS_PER_CYCLE="1", LOOKBACK_DAYS="7")

    harness.sync.run_once()
    assert client.downloaded == ["111"]

    harness.sync.run_once()

    listed_second_cycle = client.listed[-1][0]
    assert listed_second_cycle.start == datetime(2026, 7, 18, tzinfo=UTC).date()
    assert client.downloaded == ["111", "222"]


def test_it_pauses_between_downloads(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN), archives={"111": ARCHIVE, "222": ARCHIVE})
    harness = harness_for(tmp_path, client, DOWNLOAD_DELAY_SECONDS="2")

    harness.sync.run_once()

    # Once, between the two — never before the first.
    assert harness.clock.slept == [2.0]


def test_it_does_not_pause_before_the_only_download(tmp_path: Path) -> None:
    harness = harness_for(
        tmp_path,
        FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}),
        DOWNLOAD_DELAY_SECONDS="2",
    )

    harness.sync.run_once()

    assert harness.clock.slept == []


def test_being_rate_limited_ends_the_whole_cycle(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN, SWIM), archives={"111": ARCHIVE, "333": ARCHIVE})
    client.fail_download_with("222", RateLimited("429"))
    harness = harness_for(tmp_path, client)

    with pytest.raises(RateLimited):
        harness.sync.run_once()

    # The third is never asked for: retrying anything now only makes the rate limit worse.
    assert client.downloaded == ["111", "222"]
    stored = Ledger.load(harness.state_dir / LEDGER_FILENAME)
    delivered = stored.entry("111")
    assert delivered is not None
    assert delivered.status is ActivityStatus.DELIVERED
    # The cycle did not finish, so it does not count as a successful sync.
    assert stored.last_successful_sync is None


def test_being_rate_limited_while_listing_ends_the_cycle_before_any_download(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE})
    client.fail_listing_with(RateLimited("429"))
    harness = harness_for(tmp_path, client)

    with pytest.raises(RateLimited):
        harness.sync.run_once()

    assert client.downloaded == []


def test_a_rejected_session_stops_the_cycle(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN), archives={"111": ARCHIVE, "222": ARCHIVE})
    client.fail_download_with("111", AuthenticationFailed("session rejected"))
    harness = harness_for(tmp_path, client)

    with pytest.raises(AuthenticationFailed):
        harness.sync.run_once()

    assert client.downloaded == ["111"]


def test_an_unreachable_garmin_costs_one_activity_not_the_cycle(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN), archives={"111": ARCHIVE, "222": ARCHIVE})
    client.fail_download_with("111", ConnectionFailed("no route to host"), times=1)
    harness = harness_for(tmp_path, client)

    first = harness.sync.run_once()

    assert first.failed == 1
    assert first.delivered == 1
    failed = harness.ledger.entry("111")
    assert failed is not None
    assert failed.status is ActivityStatus.FAILED
    assert failed.attempts == 1
    assert failed.error is not None

    second = harness.sync.run_once()

    assert second.delivered == 1
    assert (harness.watch_dir / "111.fit").exists()


def test_an_unexpected_failure_costs_one_activity_not_the_cycle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN), archives={"111": ARCHIVE, "222": ARCHIVE})
    client.fail_download_with("111", ValueError("something nobody predicted"))
    harness = harness_for(tmp_path, client)

    result = harness.sync.run_once()

    assert result.failed == 1
    assert result.delivered == 1
    assert "Unexpected failure on activity 111" in caplog.text
    assert "Traceback" in caplog.text


def test_an_unreadable_download_is_worth_another_try(tmp_path: Path) -> None:
    # A truncated download looks exactly like this, and those are worth retrying.
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": MALFORMED_ARCHIVE}))

    result = harness.sync.run_once()

    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.status is ActivityStatus.FAILED
    assert result.failed == 1


def test_an_activity_gives_up_after_max_attempts(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE})
    client.fail_download_with("111", ConnectionFailed("no route to host"))
    harness = harness_for(tmp_path, client, MAX_ATTEMPTS="2")

    harness.sync.run_once()
    harness.sync.run_once()
    harness.sync.run_once()

    assert client.downloaded == ["111", "111"]
    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.attempts == 2


def test_an_activity_without_a_fit_falls_back_to_tcx(tmp_path: Path) -> None:
    client = FakeGarminClient(
        activities=(RIDE,),
        archives={"111": ARCHIVE_WITHOUT_FIT},
        fallbacks={"111": b"<TrainingCenterDatabase/>"},
    )
    harness = harness_for(tmp_path, client)

    result = harness.sync.run_once()

    assert (harness.watch_dir / "111.tcx").read_bytes() == b"<TrainingCenterDatabase/>"
    assert result.delivered == 1
    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.files == ("111.tcx",)


def test_an_activity_garmin_has_no_file_for_falls_back_too(tmp_path: Path) -> None:
    # Not a zip without a fit, but no download at all.
    client = FakeGarminClient(activities=(RIDE,), fallbacks={"111": b"<gpx/>"})
    harness = harness_for(tmp_path, client, FALLBACK_FORMAT="gpx")

    harness.sync.run_once()

    assert (harness.watch_dir / "111.gpx").read_bytes() == b"<gpx/>"


def test_an_activity_with_no_file_anywhere_is_recorded_as_such_permanently(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE_WITHOUT_FIT}))

    result = harness.sync.run_once()

    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.status is ActivityStatus.NO_FILE
    assert entry.status.is_terminal is True
    assert result.without_file == 1

    harness.sync.run_once()

    assert harness.client.downloaded == ["111"]


def test_the_fallback_can_be_turned_off(tmp_path: Path) -> None:
    client = FakeGarminClient(
        activities=(RIDE,),
        archives={"111": ARCHIVE_WITHOUT_FIT},
        fallbacks={"111": b"<TrainingCenterDatabase/>"},
    )
    harness = harness_for(tmp_path, client, FALLBACK_FORMAT="none")

    result = harness.sync.run_once()

    assert client.fallbacks_downloaded == []
    assert result.without_file == 1


def test_an_activity_deleted_in_garmin_is_not_chased_with_a_fallback(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE,), fallbacks={"111": b"<gpx/>"})
    client.fail_download_with("111", ActivityNotFound("404"))
    harness = harness_for(tmp_path, client)

    result = harness.sync.run_once()

    assert client.fallbacks_downloaded == []
    assert result.without_file == 1


def test_a_missing_fallback_settles_the_activity(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE_WITHOUT_FIT}))

    result = harness.sync.run_once()

    assert harness.client.fallbacks_downloaded == [("111", FallbackFormat.TCX)]
    assert result.without_file == 1


def test_a_dry_run_downloads_nothing_and_leaves_no_trace(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    harness = harness_for(
        tmp_path,
        FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}),
        DRY_RUN="true",
    )

    result = harness.sync.run_once()

    assert harness.client.downloaded == []
    assert list(harness.watch_dir.iterdir()) == []
    assert (harness.state_dir / LEDGER_FILENAME).exists() is False
    assert result.delivered == 0
    assert result.backlog == 1
    assert "Would download activity 111" in caplog.text


def test_a_dry_run_writes_nothing_even_when_there_is_nothing_to_do(tmp_path: Path) -> None:
    # Not even the "this cycle succeeded" mark: a dry run has to be free of consequences.
    harness = harness_for(tmp_path, FakeGarminClient(), SINCE="2027-01-01", DRY_RUN="true")

    harness.sync.run_once()

    assert (harness.state_dir / LEDGER_FILENAME).exists() is False


def test_a_file_already_in_the_watch_folder_still_counts_as_delivered(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}))
    (harness.watch_dir / "111.fit").write_bytes(b"put there by hand")

    result = harness.sync.run_once()

    assert (harness.watch_dir / "111.fit").read_bytes() == b"put there by hand"
    assert result.delivered == 1


def test_a_long_history_is_listed_in_pages_with_a_pause_between_them(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE})
    harness = harness_for(tmp_path, client, SINCE="2026-01-01", DOWNLOAD_DELAY_SECONDS="2")

    harness.sync.run_once()

    pages = [window for window, _ in client.listed]
    assert len(pages) == 7
    assert pages[0].start == datetime(2026, 1, 1, tzinfo=UTC).date()
    assert pages[-1].end == NOW.date()
    # One pause between each pair of pages, and none before the single download.
    assert harness.clock.slept == [2.0] * 6


def test_there_is_nothing_to_do_before_since_arrives(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(), SINCE="2027-01-01")

    result = harness.sync.run_once()

    assert harness.client.listed == []
    assert result == type(result)()
    assert Ledger.load(harness.state_dir / LEDGER_FILENAME).last_successful_sync == NOW


def test_since_now_is_resolved_once_and_survives_a_restart(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(), SINCE="now")
    harness.sync.run_once()

    harness.clock.advance(86400)
    harness.sync.run_once()

    assert Ledger.load(harness.state_dir / LEDGER_FILENAME).resolved_since == NOW


def test_a_file_that_cannot_be_written_is_worth_another_try(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}))

    def no_space(_self: WatchFolder, _file: object) -> None:
        raise UndeliverableFile("111.fit could not be delivered: no space left on device")

    monkeypatch.setattr(WatchFolder, "deliver", no_space)

    result = harness.sync.run_once()

    assert result.failed == 1
    entry = harness.ledger.entry("111")
    assert entry is not None
    assert entry.status is ActivityStatus.FAILED


def test_a_shutdown_lands_between_activities_rather_than_after_the_batch(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN, SWIM), archives={"111": ARCHIVE, "222": ARCHIVE, "333": ARCHIVE})
    harness = harness_for(tmp_path, client)
    stopping: list[str] = []

    sync = Sync(
        harness.sync_config,
        harness.ledger,
        client,
        harness.watch_folder,
        harness.clock,
        should_stop=lambda: bool(stopping),
    )

    def stop_after_the_first(name: str) -> None:
        stopping.append(name)

    client.on_download = stop_after_the_first
    result = sync.run_once()

    # One file finished, the rest still recorded as waiting rather than lost.
    assert client.downloaded == ["111"]
    assert result.delivered == 1
    waiting = [entry.activity_id for entry in harness.ledger.entries() if not entry.status.is_terminal]
    assert waiting == ["222", "333"]
    assert Ledger.load(harness.state_dir / LEDGER_FILENAME).entry("111") is not None


def test_the_cycle_summary_reads_as_a_sentence(tmp_path: Path) -> None:
    harness = harness_for(tmp_path, FakeGarminClient(activities=(RIDE,), archives={"111": ARCHIVE}))

    assert str(harness.sync.run_once()) == (
        "listed 1, delivered 1, no file 0, skipped 0, failed 0, 0 waiting for a later cycle"
    )


def test_the_system_clock_reports_an_aware_time_in_utc() -> None:
    clock = SystemClock()

    assert clock.now().tzinfo == UTC
    clock.sleep(0)


def test_the_command_line_runs_a_single_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    monkeypatch.setattr("dreeve_garmin_connector.cli.Authenticator", _AuthenticatorThatResumes)

    assert main(["sync-once", "--dry-run"]) == EXIT_OK
    # --dry-run reaches all the way down: nothing is written, not even the ledger.
    assert (tmp_path / "state" / LEDGER_FILENAME).exists() is False


def test_the_command_line_reports_a_cycle_it_could_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", _environment(tmp_path))
    monkeypatch.setattr("dreeve_garmin_connector.cli.Authenticator", _AuthenticatorThatCannotResume)

    # capsys rather than caplog: configure_logging installs the only handler this process has.
    assert main(["sync-once"]) == EXIT_FAILED
    assert "connector login" in capsys.readouterr().err


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "GARMIN_EMAIL": "rider@example.com",
        "GARMINTOKENS": str(tmp_path / "tokens"),
        "WATCH_DIR": str(tmp_path / "watch"),
        "STATE_DIR": str(tmp_path / "state"),
        "SINCE": "-7d",
    }


class _AuthenticatorThatResumes:
    def __init__(self, config: Config, secrets: Secrets) -> None:
        self._config = config
        self._secrets = secrets

    def resume(self) -> StubGarminSession:
        return StubGarminSession(activities=LISTING)


class _AuthenticatorThatCannotResume:
    def __init__(self, config: Config, secrets: Secrets) -> None:
        self._config = config
        self._secrets = secrets

    def resume(self) -> StubGarminSession:
        raise AuthenticationRequired(NO_SESSION_INSTRUCTION)


def test_only_the_configured_activity_types_are_asked_for(tmp_path: Path) -> None:
    client = FakeGarminClient(activities=(RIDE, RUN), archives={"111": ARCHIVE, "222": ARCHIVE})
    harness = harness_for(tmp_path, client, ACTIVITY_TYPES="cycling")

    harness.sync.run_once()

    assert client.listed[0][1] == ("cycling",)
    assert client.downloaded == ["111"]
    # Excluded activities are not recorded at all, so widening ACTIVITY_TYPES later still picks them up.
    assert harness.ledger.entry("222") is None
