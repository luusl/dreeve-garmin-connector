from datetime import UTC, date, datetime, timedelta, timezone
from itertools import pairwise

import pytest

from dreeve_garmin_connector.config import InvalidConfiguration, Since
from dreeve_garmin_connector.window import Window, chunked, incremental_window, sync_start

NOW = datetime(2026, 7, 25, 10, 12, tzinfo=UTC)
TODAY = date(2026, 7, 25)


def test_a_window_knows_how_many_days_it_covers() -> None:
    assert Window(date(2026, 1, 1), date(2026, 1, 1)).days == 1
    assert Window(date(2026, 1, 1), date(2026, 1, 30)).days == 30


def test_a_window_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValueError, match="cannot end before it starts"):
        Window(date(2026, 1, 2), date(2026, 1, 1))


def test_a_window_reads_as_a_date_range() -> None:
    assert str(Window(date(2026, 1, 1), date(2026, 1, 30))) == "2026-01-01..2026-01-30"


def test_an_already_resolved_since_wins_over_the_configured_one() -> None:
    # Re-resolving `SINCE=now` on every boot would skip everything recorded while we were down.
    resolved = datetime(2026, 1, 1, tzinfo=UTC)

    assert sync_start(configured=Since(timedelta(0)), already_resolved=resolved, now=NOW) == resolved


def test_the_configured_since_is_resolved_on_the_very_first_run() -> None:
    assert sync_start(configured=Since(timedelta(days=30)), already_resolved=None, now=NOW) == datetime(
        2026, 6, 25, 10, 12, tzinfo=UTC
    )


def test_the_first_run_refuses_to_guess_how_far_back_to_reach() -> None:
    with pytest.raises(InvalidConfiguration) as raised:
        sync_start(configured=None, already_resolved=None, now=NOW)

    assert "SINCE is required on the first run" in str(raised.value)


def test_a_first_cycle_lists_everything_from_since_until_today() -> None:
    window = incremental_window(since=datetime(2026, 7, 1, tzinfo=UTC), last_synced_at=None, lookback_days=7, now=NOW)

    assert window == Window(date(2026, 7, 1), TODAY)


def test_a_later_cycle_reaches_back_over_the_lookback_days() -> None:
    window = incremental_window(
        since=datetime(2026, 1, 1, tzinfo=UTC),
        last_synced_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        lookback_days=7,
        now=NOW,
    )

    assert window == Window(date(2026, 7, 17), TODAY)


def test_the_lookback_never_reaches_back_past_since() -> None:
    window = incremental_window(
        since=datetime(2026, 7, 20, tzinfo=UTC),
        last_synced_at=datetime(2026, 7, 22, tzinfo=UTC),
        lookback_days=30,
        now=NOW,
    )

    assert window == Window(date(2026, 7, 20), TODAY)


def test_without_a_lookback_a_cycle_only_lists_from_the_last_sync() -> None:
    window = incremental_window(
        since=datetime(2026, 1, 1, tzinfo=UTC),
        last_synced_at=datetime(2026, 7, 24, 9, 0, tzinfo=UTC),
        lookback_days=0,
        now=NOW,
    )

    assert window == Window(date(2026, 7, 24), TODAY)


def test_a_since_of_today_lists_a_single_day() -> None:
    window = incremental_window(since=NOW, last_synced_at=None, lookback_days=7, now=NOW)

    assert window == Window(TODAY, TODAY)


def test_there_is_nothing_to_list_when_since_lies_in_the_future() -> None:
    window = incremental_window(since=datetime(2027, 1, 1, tzinfo=UTC), last_synced_at=None, lookback_days=7, now=NOW)

    assert window is None


@pytest.mark.parametrize(
    ("since", "expected_start"),
    [
        (datetime(2026, 7, 25, 1, 0, tzinfo=timezone(timedelta(hours=2))), date(2026, 7, 24)),
        (datetime(2026, 7, 25, 1, 0, tzinfo=UTC), date(2026, 7, 25)),
    ],
)
def test_the_calendar_day_is_read_in_utc(since: datetime, expected_start: date) -> None:
    # 01:00 at +02:00 is still the previous day in UTC, and every comparison downstream is UTC.
    window = incremental_window(since=since, last_synced_at=None, lookback_days=7, now=NOW)

    assert window is not None
    assert window.start == expected_start


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (
            Window(date(2026, 1, 1), date(2026, 1, 1)),
            (Window(date(2026, 1, 1), date(2026, 1, 1)),),
        ),
        (
            Window(date(2026, 1, 1), date(2026, 1, 7)),
            (Window(date(2026, 1, 1), date(2026, 1, 7)),),
        ),
        (
            Window(date(2026, 1, 1), date(2026, 1, 30)),
            (Window(date(2026, 1, 1), date(2026, 1, 30)),),
        ),
        (
            Window(date(2026, 1, 1), date(2026, 1, 31)),
            (Window(date(2026, 1, 1), date(2026, 1, 30)), Window(date(2026, 1, 31), date(2026, 1, 31))),
        ),
        (
            Window(date(2026, 1, 1), date(2026, 3, 1)),
            (
                Window(date(2026, 1, 1), date(2026, 1, 30)),
                Window(date(2026, 1, 31), date(2026, 3, 1)),
            ),
        ),
    ],
)
def test_it_pages_a_window_into_thirty_day_chunks(window: Window, expected: tuple[Window, ...]) -> None:
    assert chunked(window) == expected


def test_the_pages_cover_the_window_exactly_once() -> None:
    window = Window(date(2021, 1, 1), TODAY)

    pages = chunked(window)

    assert pages[0].start == window.start
    assert pages[-1].end == window.end
    assert sum(page.days for page in pages) == window.days
    for earlier, later in pairwise(pages):
        # Contiguous: no day listed twice, no day missed.
        assert later.start == earlier.end + timedelta(days=1)


def test_the_page_size_is_adjustable() -> None:
    pages = chunked(Window(date(2026, 1, 1), date(2026, 1, 10)), chunk_days=4)

    assert pages == (
        Window(date(2026, 1, 1), date(2026, 1, 4)),
        Window(date(2026, 1, 5), date(2026, 1, 8)),
        Window(date(2026, 1, 9), date(2026, 1, 10)),
    )


@pytest.mark.parametrize("chunk_days", [0, -1])
def test_a_page_has_to_span_at_least_a_day(chunk_days: int) -> None:
    with pytest.raises(ValueError, match="at least one day"):
        chunked(Window(date(2026, 1, 1), date(2026, 1, 31)), chunk_days=chunk_days)
