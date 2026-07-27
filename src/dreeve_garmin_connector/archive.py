"""Turning a Garmin `ORIGINAL` download into the files that belong in the watch folder.

Garmin hands back a zip, not a FIT file. Almost always it holds exactly one `.fit`, but it can hold
several, none at all, or entries with names we have no business trusting — so nothing from inside
the archive is ever used as a path.
"""

import logging
import zipfile
from dataclasses import dataclass
from io import BytesIO

FIT_SUFFIX = ".fit"

logger = logging.getLogger(__name__)


class InvalidArchive(Exception):
    """The bytes are not a readable zip. A truncated download looks exactly like this, so it is worth retrying."""


@dataclass(frozen=True)
class ActivityFile:
    name: str
    contents: bytes


def extract_fit_files(archive: bytes, activity_id: str) -> tuple[ActivityFile, ...]:
    """Every `.fit` in the archive, named after the activity. An archive without one comes back empty."""
    try:
        with zipfile.ZipFile(BytesIO(archive)) as bundle:
            entries = sorted((info for info in bundle.infolist() if _is_fit(info)), key=lambda info: _basename(info))
            contents = [bundle.read(info) for info in entries]
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as exception:
        raise InvalidArchive(f"The archive for activity {activity_id} could not be read: {exception}.") from exception

    if not contents:
        logger.debug("Archive for activity %s holds no .fit file", activity_id)

    # Named after the activity rather than after the entry: the activity id is the one name that is
    # guaranteed unique, predictable and free of anything resembling a path.
    return tuple(
        ActivityFile(name=_file_name(activity_id, index, len(contents)), contents=payload)
        for index, payload in enumerate(contents)
    )


def _is_fit(info: zipfile.ZipInfo) -> bool:
    return not info.is_dir() and _basename(info).lower().endswith(FIT_SUFFIX)


def _basename(info: zipfile.ZipInfo) -> str:
    """Only ever the last segment: an entry called `../../etc/passwd.fit` must not be able to point anywhere."""
    return info.filename.replace("\\", "/").rsplit("/", 1)[-1]


def _file_name(activity_id: str, index: int, total: int) -> str:
    if total == 1:
        return f"{activity_id}{FIT_SUFFIX}"

    return f"{activity_id}_{index + 1}{FIT_SUFFIX}"
