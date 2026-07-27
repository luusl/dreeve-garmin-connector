import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dreeve_garmin_connector.config import Config
from dreeve_garmin_connector.ledger import ActivityStatus
from dreeve_garmin_connector.status import Status, StatusServer
from dreeve_garmin_connector.sync import CycleResult
from tests.stubs import FakeClock

NOW = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)
POLL_INTERVAL = 3600
A_CYCLE = CycleResult(listed=4, delivered=2, failed=1, skipped=0, without_file=1, backlog=7)
COUNTS = {ActivityStatus.DELIVERED: 12, ActivityStatus.PENDING: 7, ActivityStatus.FAILED: 1}


def test_a_fresh_container_is_healthy() -> None:
    status = Status(POLL_INTERVAL, NOW)

    assert status.is_healthy(NOW) is True


def test_it_stays_healthy_while_cycles_keep_finishing() -> None:
    status = Status(POLL_INTERVAL, NOW)
    status.cycle_succeeded(A_CYCLE, COUNTS, NOW + timedelta(hours=2))

    assert status.is_healthy(NOW + timedelta(hours=4)) is True


def test_it_turns_unhealthy_after_three_intervals_without_a_finished_cycle() -> None:
    status = Status(POLL_INTERVAL, NOW)
    status.cycle_succeeded(A_CYCLE, COUNTS, NOW)

    assert status.is_healthy(NOW + timedelta(seconds=POLL_INTERVAL * 3)) is True
    assert status.is_healthy(NOW + timedelta(seconds=POLL_INTERVAL * 3 + 1)) is False


def test_a_container_that_never_managed_a_cycle_turns_unhealthy_too() -> None:
    status = Status(POLL_INTERVAL, NOW)

    assert status.is_healthy(NOW + timedelta(seconds=POLL_INTERVAL * 3 + 1)) is False


def test_broken_authentication_is_unhealthy_immediately() -> None:
    # The likeliest real failure, and otherwise a silent one: the container runs, nothing arrives.
    status = Status(POLL_INTERVAL, NOW)
    status.cycle_succeeded(A_CYCLE, COUNTS, NOW)

    status.authentication_failed("No valid Garmin session")

    assert status.is_healthy(NOW) is False
    assert status.to_dict(NOW)["authentication"] == "No valid Garmin session"


def test_a_finished_cycle_clears_an_earlier_authentication_problem() -> None:
    status = Status(POLL_INTERVAL, NOW)
    status.authentication_failed("No valid Garmin session")

    status.cycle_succeeded(A_CYCLE, COUNTS, NOW)

    assert status.is_healthy(NOW) is True
    assert status.to_dict(NOW)["authentication"] == "ok"


def test_it_reports_everything_worth_knowing() -> None:
    status = Status(POLL_INTERVAL, NOW)
    status.cycle_succeeded(A_CYCLE, COUNTS, NOW)
    status.sleeping_until(NOW + timedelta(hours=1))

    assert status.to_dict(NOW) == {
        "healthy": True,
        "startedAt": NOW.isoformat(),
        "cycles": 1,
        "lastSuccessfulSync": NOW.isoformat(),
        "nextRunAt": (NOW + timedelta(hours=1)).isoformat(),
        "backoffSeconds": 0.0,
        "authentication": "ok",
        "lastError": None,
        "lastCycle": {
            "listed": 4,
            "delivered": 2,
            "failed": 1,
            "skipped": 0,
            "withoutFile": 1,
            "backlog": 7,
        },
        "backlog": 7,
        "activities": {"delivered": 12, "pending": 7, "failed": 1},
    }


def test_a_brand_new_container_reports_no_cycle_yet() -> None:
    reported = Status(POLL_INTERVAL, NOW).to_dict(NOW)

    assert reported["lastCycle"] is None
    assert reported["lastSuccessfulSync"] is None
    assert reported["backlog"] == 0
    assert reported["cycles"] == 0


def test_it_reports_the_backoff_it_is_serving() -> None:
    status = Status(POLL_INTERVAL, NOW)

    status.rate_limited(7200.0, "Garmin rate-limited us")

    reported = status.to_dict(NOW)
    assert reported["backoffSeconds"] == 7200.0
    assert reported["lastError"] == "Garmin rate-limited us"
    assert reported["cycles"] == 1


def test_a_failed_cycle_is_reported_without_pretending_it_succeeded() -> None:
    status = Status(POLL_INTERVAL, NOW)

    status.cycle_failed("the watch folder went away")

    assert status.to_dict(NOW)["lastError"] == "the watch folder went away"
    assert status.to_dict(NOW)["lastSuccessfulSync"] is None


@pytest.fixture
def served() -> Iterator[tuple[StatusServer, Status]]:
    status = Status(POLL_INTERVAL, NOW)
    # Port 0: the operating system picks a free one, so tests never collide.
    server = StatusServer("127.0.0.1:0", status, FakeClock(NOW))
    server.start()
    yield server, status
    server.stop()


def test_healthz_answers_200_while_all_is_well(served: tuple[StatusServer, Status]) -> None:
    server, status = served
    status.cycle_succeeded(A_CYCLE, COUNTS, NOW)

    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/healthz", timeout=5) as response:
        assert response.status == 200
        assert json.loads(response.read())["healthy"] is True


def test_healthz_answers_503_when_authentication_is_broken(served: tuple[StatusServer, Status]) -> None:
    server, status = served
    status.authentication_failed("No valid Garmin session")

    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/healthz", timeout=5)

    assert raised.value.code == 503


def test_status_answers_the_whole_picture(served: tuple[StatusServer, Status]) -> None:
    server, status = served
    status.cycle_succeeded(A_CYCLE, COUNTS, NOW)

    with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/status", timeout=5) as response:
        reported = json.loads(response.read())

    assert response.status == 200
    assert reported["lastCycle"]["delivered"] == 2
    assert reported["activities"]["delivered"] == 12
    assert reported["authentication"] == "ok"


def test_anything_else_is_a_404(served: tuple[StatusServer, Status]) -> None:
    server, _ = served

    with pytest.raises(urllib.error.HTTPError) as raised:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=5)

    assert raised.value.code == 404


def test_stopping_a_server_that_never_started_is_harmless() -> None:
    StatusServer("127.0.0.1:0", Status(POLL_INTERVAL, NOW), FakeClock(NOW)).stop()


def test_the_status_endpoint_can_be_turned_off(tmp_path: Path) -> None:
    config = Config.from_env({"GARMIN_EMAIL": "rider@example.com", "HTTP_ADDR": "off", "STATE_DIR": str(tmp_path)})

    assert config.http_addr is None
