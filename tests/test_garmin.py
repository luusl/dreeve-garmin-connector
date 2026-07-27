import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectInvalidFileFormatError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from dreeve_garmin_connector.config import FallbackFormat
from dreeve_garmin_connector.garmin import (
    Activity,
    ActivityNotFound,
    AuthenticationFailed,
    ConnectionFailed,
    GarminClient,
    GarminConnectClient,
    GarminError,
    NoActivityFile,
    RateLimited,
    UnexpectedResponse,
)
from dreeve_garmin_connector.window import Window
from tests.stubs import FakeClock, FakeGarminClient, StubGarminApi

FIXTURES = Path(__file__).parent / "fixtures"
LISTING = json.loads((FIXTURES / "garmin-activities.json").read_text(encoding="utf-8"))
WINDOW = Window(date(2026, 7, 25), date(2026, 7, 26))


def test_it_turns_a_garmin_listing_into_activities() -> None:
    client = GarminConnectClient(StubGarminApi(activities=LISTING))

    activities = client.list_activities(WINDOW)

    assert activities == (
        Activity(
            activity_id="12345678901",
            start_time_gmt=datetime(2026, 7, 25, 6, 13, 42, tzinfo=UTC),
            activity_type="cycling",
            name="Morning Ride",
        ),
        Activity(
            activity_id="12345678902",
            start_time_gmt=datetime(2026, 7, 25, 18, 2, tzinfo=UTC),
            activity_type="running",
            name="Evening Run",
        ),
        Activity(
            activity_id="12345678903",
            start_time_gmt=datetime(2026, 7, 26, 5, 30, tzinfo=UTC),
            activity_type="lap_swimming",
            name="",
        ),
    )


def test_it_asks_for_the_window_as_dates_oldest_first() -> None:
    api = StubGarminApi(activities=LISTING)

    GarminConnectClient(api).list_activities(WINDOW)

    assert api.listings == [("2026-07-25", "2026-07-26", None, "asc")]


def test_a_single_activity_type_is_left_to_garmin_to_filter() -> None:
    api = StubGarminApi(activities=[LISTING[0]])

    activities = GarminConnectClient(api).list_activities(WINDOW, activity_types=("cycling",))

    assert api.listings == [("2026-07-25", "2026-07-26", "cycling", "asc")]
    assert [activity.activity_type for activity in activities] == ["cycling"]


def test_several_activity_types_are_filtered_here_because_garmin_takes_only_one() -> None:
    api = StubGarminApi(activities=LISTING)

    activities = GarminConnectClient(api).list_activities(WINDOW, activity_types=("cycling", "running"))

    assert api.listings == [("2026-07-25", "2026-07-26", None, "asc")]
    assert [activity.activity_id for activity in activities] == ["12345678901", "12345678902"]


def test_an_activity_type_garmin_does_not_know_yields_nothing() -> None:
    client = GarminConnectClient(StubGarminApi(activities=LISTING))

    assert client.list_activities(WINDOW, activity_types=("bobsleigh",)) == ()


def test_it_refuses_to_guess_at_an_activity_it_cannot_read() -> None:
    unreadable = json.loads((FIXTURES / "garmin-activities-unreadable.json").read_text(encoding="utf-8"))
    client = GarminConnectClient(StubGarminApi(activities=unreadable))

    with pytest.raises(UnexpectedResponse) as raised:
        client.list_activities(WINDOW)

    assert "the API changed" in str(raised.value)


@pytest.mark.parametrize("timestamp", [1753424022, "yesterday", None])
def test_it_refuses_to_guess_at_a_timestamp(timestamp: object) -> None:
    client = GarminConnectClient(StubGarminApi(activities=[{**LISTING[0], "startTimeGMT": timestamp}]))

    with pytest.raises(UnexpectedResponse):
        client.list_activities(WINDOW)


def test_it_reads_an_iso_timestamp_too() -> None:
    # Belt and braces: the library has changed its date handling before.
    listing = [{**LISTING[0], "startTimeGMT": "2026-07-25T06:13:42+00:00"}]
    client = GarminConnectClient(StubGarminApi(activities=listing))

    assert client.list_activities(WINDOW)[0].start_time_gmt == datetime(2026, 7, 25, 6, 13, 42, tzinfo=UTC)


def test_it_downloads_the_original_archive() -> None:
    api = StubGarminApi(archive=b"PK archive")

    contents = GarminConnectClient(api).download_original("12345678901")

    assert contents == b"PK archive"
    assert api.downloads == [("12345678901", Garmin.ActivityDownloadFormat.ORIGINAL)]


@pytest.mark.parametrize(
    ("fallback", "expected_format"),
    [
        (FallbackFormat.TCX, Garmin.ActivityDownloadFormat.TCX),
        (FallbackFormat.GPX, Garmin.ActivityDownloadFormat.GPX),
    ],
)
def test_it_downloads_a_fallback_format(fallback: FallbackFormat, expected_format: object) -> None:
    api = StubGarminApi(archive=b"<gpx/>")

    contents = GarminConnectClient(api).download_fallback("12345678901", fallback)

    assert contents == b"<gpx/>"
    assert api.downloads == [("12345678901", expected_format)]


def test_there_is_no_such_thing_as_downloading_the_none_fallback() -> None:
    with pytest.raises(ValueError, match="there is no fallback"):
        GarminConnectClient(StubGarminApi()).download_fallback("12345678901", FallbackFormat.NONE)


@pytest.mark.parametrize(
    ("library_error", "expected"),
    [
        (GarminConnectTooManyRequestsError("429"), RateLimited),
        (GarminConnectAuthenticationError("rejected"), AuthenticationFailed),
        (GarminConnectConnectionError("no route to host"), ConnectionFailed),
        (GarminConnectNotFoundError("404"), ActivityNotFound),
        (GarminConnectInvalidFileFormatError("no file"), NoActivityFile),
    ],
)
def test_it_translates_every_library_failure_into_one_this_connector_acts_on(
    library_error: Exception, expected: type[GarminError]
) -> None:
    # These are handled very differently: one means back off for hours, another means stop and ask a
    # human, a third means try again next cycle. Collapsing them is how an account gets blocked.
    client = GarminConnectClient(StubGarminApi(error=library_error))

    with pytest.raises(expected) as raised:
        client.list_activities(WINDOW)

    assert raised.value.__cause__ is library_error


@pytest.mark.parametrize(
    ("library_error", "expected"),
    [
        (GarminConnectTooManyRequestsError("429"), RateLimited),
        (GarminConnectNotFoundError("404"), ActivityNotFound),
    ],
)
def test_a_download_failure_is_translated_as_well(library_error: Exception, expected: type[GarminError]) -> None:
    client = GarminConnectClient(StubGarminApi(error=library_error))

    with pytest.raises(expected):
        client.download_original("12345678901")


def test_a_deleted_activity_is_a_missing_file_rather_than_a_failure() -> None:
    # It was listed and then removed in Garmin; no format will ever produce a file for it.
    assert issubclass(ActivityNotFound, NoActivityFile)


def test_the_fake_can_stand_in_for_the_real_client() -> None:
    client: GarminClient = FakeGarminClient()

    assert isinstance(client, GarminClient)


def test_the_fake_only_returns_activities_inside_the_window() -> None:
    ride = Activity("1", datetime(2026, 7, 25, 6, 0, tzinfo=UTC), "cycling", "Ride")
    run = Activity("2", datetime(2026, 8, 1, 6, 0, tzinfo=UTC), "running", "Run")
    client = FakeGarminClient(activities=(ride, run))

    assert client.list_activities(WINDOW) == (ride,)
    assert client.listed == [(WINDOW, ())]


def test_the_fake_can_be_told_to_fail_a_fixed_number_of_times() -> None:
    client = FakeGarminClient(archives={"1": b"PK"})
    client.fail_download_with("1", ConnectionFailed("no route to host"), times=1)

    with pytest.raises(ConnectionFailed):
        client.download_original("1")

    assert client.download_original("1") == b"PK"


def test_the_fake_can_be_told_to_fail_forever() -> None:
    client = FakeGarminClient()
    client.fail_listing_with(RateLimited("429"))

    for _ in range(3):
        with pytest.raises(RateLimited):
            client.list_activities(WINDOW)


def test_the_fake_clock_only_moves_when_it_is_told_to() -> None:
    clock = FakeClock(datetime(2026, 7, 25, 10, 0, tzinfo=UTC))

    clock.sleep(90)

    assert clock.slept == [90]
    assert clock.now() == datetime(2026, 7, 25, 10, 1, 30, tzinfo=UTC)
