import logging
import stat
from pathlib import Path
from typing import Any

import pytest

from dreeve_garmin_connector.archive import ActivityFile
from dreeve_garmin_connector.config import ConflictPolicy
from dreeve_garmin_connector.delivery import (
    DeliveryOutcome,
    UndeliverableFile,
    WatchFolder,
    WatchFolderUnusable,
)

ACTIVITY_FILE = ActivityFile(name="12345678901.fit", contents=b".FIT contents")


def test_it_creates_the_watch_folder_when_it_is_not_there_yet(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch" / "nested"

    WatchFolder(watch_dir).prepare()

    assert watch_dir.is_dir()


def test_it_refuses_to_start_when_the_watch_folder_is_not_a_folder(tmp_path: Path) -> None:
    # A bind mount pointing at a file instead of a directory.
    watch_dir = tmp_path / "watch"
    watch_dir.write_text("not a directory", encoding="utf-8")

    with pytest.raises(WatchFolderUnusable) as raised:
        WatchFolder(watch_dir).prepare()

    assert "could not be created" in str(raised.value)


def test_it_refuses_to_start_when_the_watch_folder_cannot_be_written_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stands in for a read-only bind mount, which the container cannot produce for itself as root.
    def read_only(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "touch", read_only)

    with pytest.raises(WatchFolderUnusable) as raised:
        WatchFolder(tmp_path / "watch").prepare()

    assert "is not writable" in str(raised.value)
    assert "PUID/PGID" in str(raised.value)


def test_it_leaves_no_probe_behind(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"

    WatchFolder(watch_dir).prepare()

    assert list(watch_dir.iterdir()) == []


def test_it_delivers_a_file(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir)
    folder.prepare()

    outcome = folder.deliver(ACTIVITY_FILE)

    assert outcome is DeliveryOutcome.WRITTEN
    assert (watch_dir / ACTIVITY_FILE.name).read_bytes() == ACTIVITY_FILE.contents
    assert list(watch_dir.glob(".*")) == []


def test_a_delivered_file_is_readable_by_whoever_imports_it(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir)
    folder.prepare()

    folder.deliver(ACTIVITY_FILE)

    assert stat.S_IMODE((watch_dir / ACTIVITY_FILE.name).stat().st_mode) == 0o644


def test_the_final_name_only_appears_once_the_file_is_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir)
    folder.prepare()

    observed: dict[str, Any] = {}
    real_replace = Path.replace

    def spy(source: Path, destination: Any) -> Path:
        observed["part_contents"] = source.read_bytes()
        observed["destination_existed"] = Path(destination).exists()
        observed["part_name"] = source.name

        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", spy)

    folder.deliver(ACTIVITY_FILE)

    # Dreeve scans this folder on its own schedule: until the rename there must be nothing to find.
    assert observed["destination_existed"] is False
    assert observed["part_contents"] == ACTIVITY_FILE.contents
    assert observed["part_name"] == f".{ACTIVITY_FILE.name}.part"


def test_the_temp_file_sits_next_to_its_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir)
    folder.prepare()

    observed: dict[str, Any] = {}
    real_replace = Path.replace

    def spy(source: Path, destination: Any) -> Path:
        observed["same_directory"] = source.parent == Path(destination).parent

        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", spy)

    folder.deliver(ACTIVITY_FILE)

    # Same directory means same filesystem, which is the only reason the rename is atomic.
    assert observed["same_directory"] is True


def test_a_failed_delivery_leaves_nothing_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir)
    folder.prepare()

    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "replace", fail)

    with pytest.raises(UndeliverableFile) as raised:
        folder.deliver(ACTIVITY_FILE)

    assert ACTIVITY_FILE.name in str(raised.value)
    assert "No space left on device" in str(raised.value)
    assert list(watch_dir.iterdir()) == []


def test_it_discards_a_partial_file_left_by_a_killed_process(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    leftover = watch_dir / f".{ACTIVITY_FILE.name}.part"
    leftover.write_bytes(b".FIT half")
    keeper = watch_dir / "22222222222.fit"
    keeper.write_bytes(b".FIT waiting to be imported")

    with caplog.at_level(logging.WARNING):
        WatchFolder(watch_dir).prepare()

    assert leftover.exists() is False
    assert keeper.exists() is True
    assert "left behind by an interrupted delivery" in caplog.text


def test_it_leaves_an_existing_file_alone(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir, on_conflict=ConflictPolicy.SKIP)
    folder.prepare()
    (watch_dir / ACTIVITY_FILE.name).write_bytes(b"already here")

    outcome = folder.deliver(ACTIVITY_FILE)

    assert outcome is DeliveryOutcome.SKIPPED
    assert (watch_dir / ACTIVITY_FILE.name).read_bytes() == b"already here"


def test_it_replaces_an_existing_file_when_told_to(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir, on_conflict=ConflictPolicy.OVERWRITE)
    folder.prepare()
    (watch_dir / ACTIVITY_FILE.name).write_bytes(b"already here")

    outcome = folder.deliver(ACTIVITY_FILE)

    assert outcome is DeliveryOutcome.WRITTEN
    assert (watch_dir / ACTIVITY_FILE.name).read_bytes() == ACTIVITY_FILE.contents


def test_a_stale_temp_file_does_not_get_in_the_way(tmp_path: Path) -> None:
    watch_dir = tmp_path / "watch"
    folder = WatchFolder(watch_dir)
    folder.prepare()
    (watch_dir / f".{ACTIVITY_FILE.name}.part").write_bytes(b".FIT half from an earlier crash")

    outcome = folder.deliver(ACTIVITY_FILE)

    assert outcome is DeliveryOutcome.WRITTEN
    assert (watch_dir / ACTIVITY_FILE.name).read_bytes() == ACTIVITY_FILE.contents
