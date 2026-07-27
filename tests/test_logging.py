import json
import logging
from collections.abc import Iterator

import pytest

from dreeve_garmin_connector.config import LogFormat
from dreeve_garmin_connector.logging_ import REDACTED, Secrets, configure_logging

PASSWORD = "sup3r-s3cret"
EMAIL = "rider@example.com"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.token-payload"


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.setLevel(level)


def test_it_redacts_a_secret_from_the_message(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.TEXT, Secrets([EMAIL, PASSWORD]))

    logging.getLogger("test").info(f"logging in with {PASSWORD}")

    output = capsys.readouterr().err
    assert PASSWORD not in output
    assert f"logging in with {REDACTED}" in output


def test_it_redacts_a_secret_that_only_appears_once_arguments_are_interpolated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("info", LogFormat.TEXT, Secrets([EMAIL]))

    logging.getLogger("test").info("logging in as %s", EMAIL)

    output = capsys.readouterr().err
    assert EMAIL not in output
    assert f"logging in as {REDACTED}" in output


def test_it_redacts_a_secret_from_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.TEXT, Secrets([PASSWORD]))

    try:
        # The library raises with whatever it was handed, so credentials do end up in tracebacks.
        raise ValueError(f"login rejected for password {PASSWORD}")
    except ValueError:
        logging.getLogger("test").exception("login failed")

    output = capsys.readouterr().err
    assert PASSWORD not in output
    assert "ValueError: login rejected for password ***" in output
    assert "Traceback (most recent call last)" in output


def test_it_redacts_a_secret_registered_after_logging_was_configured(capsys: pytest.CaptureFixture[str]) -> None:
    # Session tokens only exist after login, long after logging is set up.
    secrets = Secrets([PASSWORD])
    configure_logging("info", LogFormat.TEXT, secrets)
    secrets.add(TOKEN)

    logging.getLogger("test").info("resumed session %s", TOKEN)

    assert TOKEN not in capsys.readouterr().err


def test_it_redacts_the_longest_matching_secret_first() -> None:
    secrets = Secrets(["token", "token-with-suffix"])

    assert secrets.redact("token-with-suffix") == REDACTED


@pytest.mark.parametrize("value", [None, "", "ab"])
def test_it_ignores_values_too_short_to_be_a_credential(value: str | None) -> None:
    secrets = Secrets([value])

    assert secrets.redact("ab is a common substring") == "ab is a common substring"


def test_it_writes_json_lines_when_asked(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.JSON, Secrets([PASSWORD]))

    logging.getLogger("connector").warning("using %s", PASSWORD)

    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["level"] == "warning"
    assert payload["logger"] == "connector"
    assert payload["message"] == f"using {REDACTED}"
    assert payload["timestamp"]


def test_it_includes_a_redacted_traceback_in_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.JSON, Secrets([TOKEN]))

    try:
        raise ValueError(f"bad token {TOKEN}")
    except ValueError:
        logging.getLogger("connector").exception("cycle failed")

    payload = json.loads(capsys.readouterr().err.strip())
    assert TOKEN not in payload["exception"]
    assert payload["exception"].startswith("Traceback (most recent call last)")


def test_it_redacts_a_secret_from_a_stack_dump(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.TEXT, Secrets([PASSWORD]))

    # A stack dump quotes the source line it was raised from, credentials and all.
    logging.getLogger("test").info("connecting with %s", "sup3r-s3cret", stack_info=True)

    output = capsys.readouterr().err
    assert PASSWORD not in output
    assert "Stack (most recent call last)" in output


def test_it_includes_a_redacted_stack_dump_in_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.JSON, Secrets([PASSWORD]))

    logging.getLogger("connector").info("connecting with %s", "sup3r-s3cret", stack_info=True)

    payload = json.loads(capsys.readouterr().err.strip())
    assert PASSWORD not in payload["stack"]


def test_it_honours_the_configured_level(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("warning", LogFormat.TEXT, Secrets())

    logging.getLogger("test").info("chatter")
    logging.getLogger("test").warning("trouble")

    output = capsys.readouterr().err
    assert "chatter" not in output
    assert "trouble" in output


def test_it_replaces_handlers_instead_of_stacking_them(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("info", LogFormat.TEXT, Secrets())
    configure_logging("info", LogFormat.TEXT, Secrets())

    logging.getLogger("test").info("once")

    assert capsys.readouterr().err.count("once") == 1
