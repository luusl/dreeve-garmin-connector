import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dreeve_garmin_connector.auth import NO_SESSION_INSTRUCTION, AuthenticationRequired
from dreeve_garmin_connector.cli import EXIT_FAILED, EXIT_OK, healthcheck, main, status
from dreeve_garmin_connector.config import Config
from dreeve_garmin_connector.delivery import WatchFolder
from dreeve_garmin_connector.garmin import AuthenticationFailed, ConnectionFailed, RateLimited
from dreeve_garmin_connector.ledger import LEDGER_FILENAME, Ledger
from dreeve_garmin_connector.loop import SyncLoop, heartbeat_path
from dreeve_garmin_connector.status import HEARTBEAT_FILENAME, Status, StatusServer
from dreeve_garmin_connector.sync import CycleResult, Sync, SystemClock
from tests.stubs import FakeClock, FakeGarminClient

NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
A_CYCLE = CycleResult(listed=1, delivered=1)


class SyncStub(Sync):
    """A cycle with a script: a queue of results, exceptions, or both."""

    def __init__(self, config: Config, ledger: Ledger, outcomes: list[object] | None = None) -> None:
        super().__init__(config, ledger, FakeGarminClient(), WatchFolder(config.watch_dir), FakeClock(NOW))
        self._outcomes = list(outcomes or [])
        self.cycles = 0

    def run_once(self) -> CycleResult:
        self.cycles += 1
        outcome = self._outcomes.pop(0) if self._outcomes else A_CYCLE
        if isinstance(outcome, Exception):
            raise outcome

        assert isinstance(outcome, CycleResult)

        return outcome


def config_for(tmp_path: Path, **env: str) -> Config:
    return Config.from_env(
        {
            "GARMIN_EMAIL": "rider@example.com",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "SINCE": "-7d",
            "MAX_CYCLES": "1",
            **env,
        }
    )


def loop_for(
    config: Config, sync: Sync | None = None, sleep: Callable[[float], object] | None = None
) -> tuple[SyncLoop, Status, list[float]]:
    recorded: list[float] = []
    status = Status(config.poll_interval, NOW)
    built = sync if sync is not None else SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME))

    def factory(_should_stop: Callable[[], bool]) -> Sync:
        return built

    def record(seconds: float) -> None:
        recorded.append(seconds)
        if sleep is not None:
            sleep(seconds)

    return SyncLoop(config, factory, status, FakeClock(NOW), sleep=record), status, recorded


def test_it_runs_the_number_of_cycles_it_was_asked_for(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="3")
    sync = SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME))
    loop, _, sleeps = loop_for(config, sync)

    loop.run()

    assert sync.cycles == 3
    # Two waits for three cycles: the loop never sleeps after the last one.
    assert len(sleeps) == 2


def test_it_runs_a_cycle_immediately_rather_than_waiting_out_the_first_interval(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    sync = SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME))
    loop, _, sleeps = loop_for(config, sync)

    loop.run()

    assert sync.cycles == 1
    assert sleeps == []


def test_the_wait_is_jittered_but_stays_within_the_configured_spread(tmp_path: Path) -> None:
    loop, _, _ = loop_for(config_for(tmp_path, POLL_INTERVAL="3600", POLL_JITTER_PCT="10"))

    delays = [loop.next_delay() for _ in range(100)]

    assert all(3240 <= delay <= 3960 for delay in delays)
    # Jitter that never varies is not jitter, and every container would still hit the same second.
    assert len(set(delays)) > 1


def test_without_jitter_the_wait_is_exactly_the_interval(tmp_path: Path) -> None:
    loop, _, _ = loop_for(config_for(tmp_path, POLL_INTERVAL="900", POLL_JITTER_PCT="0"))

    assert loop.next_delay() == 900


def test_a_rate_limit_backs_off_exponentially_up_to_the_cap(tmp_path: Path) -> None:
    config = config_for(tmp_path, POLL_INTERVAL="3600", MAX_BACKOFF_SECONDS="21600", MAX_CYCLES="0")
    sync = SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME), [RateLimited("429")] * 5)
    running: list[SyncLoop] = []

    # Five rate-limited cycles, then stop before a sixth.
    def stop_after_five(_seconds: float) -> None:
        if len(sleeps) == 5:
            running[0].request_stop()

    loop, status, sleeps = loop_for(config, sync, sleep=stop_after_five)
    running.append(loop)

    loop.run()

    assert sleeps == [7200, 14400, 21600, 21600, 21600]
    assert status.to_dict(NOW)["backoffSeconds"] == 21600


def test_a_finished_cycle_clears_the_backoff(tmp_path: Path) -> None:
    config = config_for(tmp_path, POLL_INTERVAL="3600", POLL_JITTER_PCT="0", MAX_CYCLES="2")
    sync = SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME), [RateLimited("429"), A_CYCLE])
    loop, _, sleeps = loop_for(config, sync)

    loop.run()

    assert sleeps == [7200]
    assert loop.next_delay() == 3600


def test_a_missing_session_is_reported_and_tried_again_next_cycle(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="3")
    attempts: list[int] = []

    def factory(_should_stop: Callable[[], bool]) -> Sync:
        attempts.append(1)
        raise AuthenticationRequired(NO_SESSION_INSTRUCTION)

    status = Status(config.poll_interval, NOW)
    loop = SyncLoop(config, factory, status, FakeClock(NOW), sleep=lambda _seconds: None)

    loop.run()

    # Looking for a token store costs nothing, so a `login` run while we wait is picked up without a restart.
    assert len(attempts) == 3
    assert status.is_healthy(NOW) is False
    assert "connector login" in status.to_dict(NOW)["authentication"]


def test_a_rejected_session_is_never_tried_again(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    config = config_for(tmp_path, MAX_CYCLES="3")
    attempts: list[int] = []

    def factory(_should_stop: Callable[[], bool]) -> Sync:
        attempts.append(1)
        raise AuthenticationFailed("Garmin rejected the session")

    status = Status(config.poll_interval, NOW)
    loop = SyncLoop(config, factory, status, FakeClock(NOW), sleep=lambda _seconds: None)

    with caplog.at_level(logging.ERROR):
        loop.run()

    # Asking a rejecting endpoint again every hour is the retry storm that gets accounts blocked.
    assert len(attempts) == 1
    assert status.is_healthy(NOW) is False
    assert "will not try again" in caplog.text


def test_a_session_rejected_mid_cycle_is_not_resumed_either(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="3")
    sync = SyncStub(
        config,
        Ledger.load(config.state_dir / LEDGER_FILENAME),
        [AuthenticationFailed("Garmin rejected the session")],
    )
    loop, status, _ = loop_for(config, sync)

    loop.run()

    assert sync.cycles == 1
    assert status.is_healthy(NOW) is False


def test_an_ordinary_failure_does_not_end_the_daemon(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="2")
    sync = SyncStub(
        config,
        Ledger.load(config.state_dir / LEDGER_FILENAME),
        [ConnectionFailed("no route to host"), A_CYCLE],
    )
    loop, status, _ = loop_for(config, sync)

    loop.run()

    assert sync.cycles == 2
    assert status.to_dict(NOW)["lastError"] is None


def test_it_stops_when_it_is_asked_to(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="0")
    sync = SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME))
    running: list[SyncLoop] = []
    loop, _, _ = loop_for(config, sync, sleep=lambda _seconds: running[0].request_stop())
    running.append(loop)

    loop.run()

    assert sync.cycles == 1
    assert loop.stopping is True


def test_a_stop_asked_for_mid_cycle_is_seen_by_the_cycle_itself(tmp_path: Path) -> None:
    # This is what lets a shutdown land within one file rather than one full batch.
    config = config_for(tmp_path)
    seen: list[Callable[[], bool]] = []

    def factory(should_stop: Callable[[], bool]) -> Sync:
        seen.append(should_stop)
        return SyncStub(config, Ledger.load(config.state_dir / LEDGER_FILENAME))

    loop = SyncLoop(config, factory, Status(config.poll_interval, NOW), FakeClock(NOW), sleep=lambda _s: None)
    loop.run()

    assert seen[0]() is False
    loop.request_stop()
    assert seen[0]() is True


def test_it_records_a_heartbeat_for_the_healthcheck_to_read(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    loop, _, _ = loop_for(config)

    loop.run()

    assert heartbeat_path(config.state_dir).exists()


def test_a_heartbeat_that_cannot_be_written_is_only_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    config = config_for(tmp_path)
    loop, status, _ = loop_for(config)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "touch", refuse)

    with caplog.at_level(logging.WARNING):
        loop.run()

    assert "Could not write the heartbeat file" in caplog.text
    assert status.is_healthy(NOW) is True


def test_the_healthcheck_passes_while_the_heartbeat_is_fresh(tmp_path: Path) -> None:
    config = config_for(tmp_path, HTTP_ADDR="off")
    config.state_dir.mkdir(parents=True, exist_ok=True)
    heartbeat_path(config.state_dir).touch()

    assert healthcheck(config, secrets=None) == EXIT_OK  # type: ignore[arg-type]


def test_the_healthcheck_fails_when_there_is_no_heartbeat_at_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_for(tmp_path, HTTP_ADDR="off")

    assert healthcheck(config, secrets=None) == EXIT_FAILED  # type: ignore[arg-type]
    assert "No heartbeat" in capsys.readouterr().err


def test_the_healthcheck_fails_when_the_heartbeat_went_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_for(tmp_path, HTTP_ADDR="off", POLL_INTERVAL="60")
    config.state_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = heartbeat_path(config.state_dir)
    heartbeat.touch()
    stale = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    import os

    os.utime(heartbeat, (stale, stale))

    assert healthcheck(config, secrets=None) == EXIT_FAILED  # type: ignore[arg-type]
    assert "The last successful cycle was at" in capsys.readouterr().err


def test_the_healthcheck_reports_a_connector_that_is_not_answering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Nothing is listening on this port, which is exactly what a dead container looks like.
    config = config_for(tmp_path, HTTP_ADDR="127.0.0.1:9")

    assert healthcheck(config, secrets=None) == EXIT_FAILED  # type: ignore[arg-type]
    assert "Not healthy" in capsys.readouterr().err


def test_the_status_command_says_when_there_is_nothing_to_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        "os.environ",
        {
            "GARMIN_EMAIL": "rider@example.com",
            "STATE_DIR": str(tmp_path / "state"),
            "SINCE": "-7d",
            "HTTP_ADDR": "off",
        },
    )

    assert main(["status"]) == EXIT_FAILED
    assert "HTTP_ADDR is off" in capsys.readouterr().err


def test_the_daemon_refuses_to_start_without_a_usable_watch_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.write_text("a file where a folder should be", encoding="utf-8")
    monkeypatch.setattr(
        "os.environ",
        {
            "GARMIN_EMAIL": "rider@example.com",
            "WATCH_DIR": str(watch_dir),
            "STATE_DIR": str(tmp_path / "state"),
            "SINCE": "-7d",
        },
    )

    assert main(["run"]) == EXIT_FAILED
    assert "Cannot start" in capsys.readouterr().err


def test_the_daemon_serves_status_while_it_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import urllib.request

    from dreeve_garmin_connector import cli

    monkeypatch.setattr(
        "os.environ",
        {
            "GARMIN_EMAIL": "rider@example.com",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "SINCE": "-7d",
            "MAX_CYCLES": "1",
            "HTTP_ADDR": "127.0.0.1:8099",
        },
    )

    def build(config: Config, _secrets: object, _should_stop: object) -> "_AlwaysSucceedingSync":
        return _AlwaysSucceedingSync(config)

    monkeypatch.setattr(cli, "build_sync", build)

    reported: dict[str, object] = {}

    class _WatchingStatus(Status):
        def cycle_succeeded(self, result: CycleResult, counts: dict, at: datetime) -> None:  # type: ignore[type-arg]
            super().cycle_succeeded(result, counts, at)
            with urllib.request.urlopen("http://127.0.0.1:8099/status", timeout=5) as response:
                reported.update(json.loads(response.read()))

    monkeypatch.setattr(cli, "Status", _WatchingStatus)

    assert main(["run"]) == EXIT_OK
    assert reported["healthy"] is True
    assert (tmp_path / "state" / HEARTBEAT_FILENAME).exists()


class _AlwaysSucceedingSync:
    def __init__(self, config: Config) -> None:
        self.ledger = Ledger.load(config.state_dir / LEDGER_FILENAME)

    def run_once(self) -> CycleResult:
        return A_CYCLE


def test_a_stop_asked_for_during_a_cycle_ends_the_loop_without_waiting(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="0")
    running: list[SyncLoop] = []

    class StopsItself(SyncStub):
        def run_once(self) -> CycleResult:
            running[0].request_stop()
            # Asking twice must be as harmless as asking once.
            running[0].request_stop()
            return super().run_once()

    sync = StopsItself(config, Ledger.load(config.state_dir / LEDGER_FILENAME))
    loop, _, sleeps = loop_for(config, sync)
    running.append(loop)

    loop.run()

    assert sync.cycles == 1
    assert sleeps == []


def test_a_session_that_goes_missing_mid_cycle_is_reported(tmp_path: Path) -> None:
    config = config_for(tmp_path, MAX_CYCLES="1")
    sync = SyncStub(
        config,
        Ledger.load(config.state_dir / LEDGER_FILENAME),
        [AuthenticationRequired(NO_SESSION_INSTRUCTION)],
    )
    loop, status, _ = loop_for(config, sync)

    loop.run()

    assert status.is_healthy(NOW) is False
    assert "connector login" in status.to_dict(NOW)["authentication"]


def test_a_rate_limit_while_resuming_backs_off_like_any_other(tmp_path: Path) -> None:
    config = config_for(tmp_path, POLL_INTERVAL="600", MAX_CYCLES="1")

    def factory(_should_stop: Callable[[], bool]) -> Sync:
        raise RateLimited("429 on login")

    status = Status(config.poll_interval, NOW)
    loop = SyncLoop(config, factory, status, FakeClock(NOW), sleep=lambda _seconds: None)

    loop.run()

    assert status.to_dict(NOW)["backoffSeconds"] == 1200
    assert loop.next_delay() == 1200


def test_a_connector_that_cannot_start_a_sync_says_so_and_carries_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = config_for(tmp_path, MAX_CYCLES="2")

    def factory(_should_stop: Callable[[], bool]) -> Sync:
        raise ConnectionFailed("no route to host")

    status = Status(config.poll_interval, NOW)
    loop = SyncLoop(config, factory, status, FakeClock(NOW), sleep=lambda _seconds: None)

    with caplog.at_level(logging.ERROR):
        loop.run()

    assert "Could not start a sync" in caplog.text
    assert status.to_dict(NOW)["lastError"] == "no route to host"


def test_the_healthcheck_passes_against_a_connector_that_is_answering(tmp_path: Path) -> None:
    status = Status(3600, datetime.now(UTC))
    server = StatusServer("127.0.0.1:0", status, SystemClock())
    server.start()
    try:
        config = config_for(tmp_path, HTTP_ADDR=f"127.0.0.1:{server.port}")

        assert healthcheck(config, secrets=None) == EXIT_OK  # type: ignore[arg-type]
    finally:
        server.stop()


def test_the_healthcheck_fails_against_a_connector_that_says_it_is_unhealthy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    status = Status(3600, datetime.now(UTC))
    status.authentication_failed("No valid Garmin session")
    server = StatusServer("127.0.0.1:0", status, SystemClock())
    server.start()
    try:
        config = config_for(tmp_path, HTTP_ADDR=f"127.0.0.1:{server.port}")

        assert healthcheck(config, secrets=None) == EXIT_FAILED  # type: ignore[arg-type]
        assert "Not healthy" in capsys.readouterr().err
    finally:
        server.stop()


def test_the_status_command_prints_what_the_connector_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    reporting = Status(3600, datetime.now(UTC))
    reporting.cycle_succeeded(A_CYCLE, {}, datetime.now(UTC))
    server = StatusServer("127.0.0.1:0", reporting, SystemClock())
    server.start()
    try:
        config = config_for(tmp_path, HTTP_ADDR=f"127.0.0.1:{server.port}")

        assert status(config, secrets=None) == EXIT_OK  # type: ignore[arg-type]
    finally:
        server.stop()

    assert json.loads(capsys.readouterr().out)["lastCycle"]["delivered"] == 1


def test_the_status_command_reports_a_connector_that_is_not_answering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_for(tmp_path, HTTP_ADDR="127.0.0.1:9")

    assert status(config, secrets=None) == EXIT_FAILED  # type: ignore[arg-type]
    assert "Could not reach the status endpoint" in capsys.readouterr().err


def test_the_daemon_runs_with_the_status_endpoint_turned_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dreeve_garmin_connector import cli

    monkeypatch.setattr(
        "os.environ",
        {
            "GARMIN_EMAIL": "rider@example.com",
            "WATCH_DIR": str(tmp_path / "watch"),
            "STATE_DIR": str(tmp_path / "state"),
            "SINCE": "-7d",
            "MAX_CYCLES": "1",
            "HTTP_ADDR": "off",
        },
    )

    def build(config: Config, _secrets: object, _should_stop: object) -> "_AlwaysSucceedingSync":
        return _AlwaysSucceedingSync(config)

    monkeypatch.setattr(cli, "build_sync", build)

    assert main(["run"]) == EXIT_OK
    # With no endpoint to ask, the heartbeat file is the only thing the healthcheck can read.
    assert heartbeat_path(tmp_path / "state").exists()
