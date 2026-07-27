from pathlib import Path

import pytest

from dreeve_garmin_connector.archive import ActivityFile, InvalidArchive, extract_fit_files

# The archives were built with zipfile.writestr(); `unzip -l` shows what each one holds.
FIXTURES = Path(__file__).parent / "fixtures"
ACTIVITY_ID = "12345678901"


def test_it_extracts_the_one_fit_file_an_archive_normally_holds() -> None:
    files = extract_fit_files((FIXTURES / "activity-single-fit.zip").read_bytes(), ACTIVITY_ID)

    assert files == (ActivityFile(name=f"{ACTIVITY_ID}.fit", contents=b".FIT single"),)


def test_it_numbers_the_files_when_an_archive_holds_several() -> None:
    files = extract_fit_files((FIXTURES / "activity-two-fits.zip").read_bytes(), ACTIVITY_ID)

    # Ordered by entry name, not by archive order (which has activity_b first), so a re-download
    # produces the same names as the first attempt.
    assert files == (
        ActivityFile(name=f"{ACTIVITY_ID}_1.fit", contents=b".FIT first"),
        ActivityFile(name=f"{ACTIVITY_ID}_2.fit", contents=b".FIT second"),
    )


def test_an_archive_without_a_fit_file_comes_back_empty() -> None:
    # Manually entered activities have no device file; that is a fact about the activity, not a failure.
    assert extract_fit_files((FIXTURES / "activity-no-fit.zip").read_bytes(), ACTIVITY_ID) == ()


def test_it_recognises_a_fit_file_whatever_the_case_and_ignores_everything_else() -> None:
    files = extract_fit_files((FIXTURES / "activity-mixed-entries.zip").read_bytes(), ACTIVITY_ID)

    assert files == (ActivityFile(name=f"{ACTIVITY_ID}.fit", contents=b".FIT uppercase"),)


def test_an_entry_that_tries_to_escape_the_archive_cannot_name_the_file() -> None:
    # The archive holds `../../etc/passwd.fit`. Its contents are still a fit file; its name is not.
    files = extract_fit_files((FIXTURES / "activity-zip-slip.zip").read_bytes(), ACTIVITY_ID)

    assert files == (ActivityFile(name=f"{ACTIVITY_ID}.fit", contents=b".FIT traversal"),)


@pytest.mark.parametrize("fixture", ["activity-zip-slip.zip", "activity-two-fits.zip", "activity-single-fit.zip"])
def test_an_extracted_name_is_never_a_path(fixture: str) -> None:
    files = extract_fit_files((FIXTURES / fixture).read_bytes(), ACTIVITY_ID)

    assert files
    for file in files:
        assert "/" not in file.name
        assert "\\" not in file.name
        assert ".." not in file.name
        assert file.name.startswith(ACTIVITY_ID)


def test_it_reports_bytes_that_are_not_an_archive_at_all() -> None:
    with pytest.raises(InvalidArchive) as raised:
        extract_fit_files((FIXTURES / "activity-malformed.zip").read_bytes(), ACTIVITY_ID)

    assert f"archive for activity {ACTIVITY_ID} could not be read" in str(raised.value)


def test_it_reports_an_empty_download() -> None:
    with pytest.raises(InvalidArchive):
        extract_fit_files(b"", ACTIVITY_ID)
