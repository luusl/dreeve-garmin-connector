"""Getting a Garmin session — and, far more importantly, refusing to get one the wrong way.

Logging in is the operation Garmin rate-limits, not reading data. A daemon that logs in every cycle,
or retries a login that just failed, gets the account blocked. So the daemon never logs in at all:
it resumes a session a human created once, and when there is none it says so and waits.

That rule is enforced structurally rather than by discipline. `garminconnect.Garmin.login()` falls
back to a credential login whenever the token store fails to load, so the only reliable way to make
a password login impossible is to withhold the password from the client entirely — which is exactly
what `resume()` does.
"""

import json
import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, Protocol

from garminconnect import Garmin

from dreeve_garmin_connector.config import Config, InvalidConfiguration
from dreeve_garmin_connector.garmin import AuthenticationFailed, GarminApi, translated_errors
from dreeve_garmin_connector.logging_ import Secrets

LOGIN_COMMAND = "docker compose run --rm garmin-connector login"
NO_SESSION_INSTRUCTION = (
    f"No valid Garmin session. Run `{LOGIN_COMMAND}` once to create one, then start the container again. "
    f"This connector will not log in on its own: repeated logins are what get a Garmin account rate-limited."
)

logger = logging.getLogger(__name__)


class AuthenticationRequired(AuthenticationFailed):
    """There is nothing to resume and nothing we are allowed to try. A human has to run the login command."""


class GarminSession(GarminApi, Protocol):
    """A logged-in `garminconnect.Garmin`: the API surface plus the call that gets us there."""

    def login(self, tokenstore: str | None = ...) -> Any: ...


SessionFactory = Callable[..., GarminSession]


class Authenticator:
    def __init__(self, config: Config, secrets: Secrets, session_factory: SessionFactory = Garmin) -> None:
        self._config = config
        self._secrets = secrets
        self._new_session = session_factory
        self._password_attempt_spent = False

    def resume(self) -> GarminSession:
        """Used by the daemon. Reaches for the token store and, by default, nothing else."""
        if self._has_stored_session():
            return self._start(password=None, doing="resuming the stored session")

        if self._config.allow_password_login and not self._password_attempt_spent:
            # One attempt for the whole life of the process — never once per cycle.
            self._password_attempt_spent = True
            logger.warning("No stored session; making the single password login ALLOW_PASSWORD_LOGIN permits")
            return self._start(password=self._config.garmin_password, doing="logging in with a password")

        # Deliberately before anything is constructed: no client, no request, no failed login to count.
        raise AuthenticationRequired(NO_SESSION_INSTRUCTION)

    def log_in(self, prompt_mfa: Callable[[], str]) -> GarminSession:
        """Used by the `login` command, with a human at the keyboard to answer the MFA prompt."""
        if not self._config.garmin_password:
            raise InvalidConfiguration(
                ["GARMIN_PASSWORD (or GARMIN_PASSWORD_FILE) is required to log in; it is not needed afterwards."]
            )

        return self._start(
            password=self._config.garmin_password,
            doing="logging in",
            prompt_mfa=prompt_mfa,
        )

    def _start(
        self,
        *,
        password: str | None,
        doing: str,
        prompt_mfa: Callable[[], str] | None = None,
    ) -> GarminSession:
        session = self._new_session(
            email=self._config.garmin_email,
            password=password,
            is_cn=self._config.garmin_is_cn,
            prompt_mfa=prompt_mfa,
        )

        logger.info("%s (token store: %s)", doing.capitalize(), self._config.garmin_tokens)
        with translated_errors(doing):
            session.login(str(self._config.garmin_tokens))

        self._remember_token_secrets()

        return session

    def _has_stored_session(self) -> bool:
        """GARMINTOKENS may be a file or a directory depending on the library version; both are handled."""
        tokens = self._config.garmin_tokens
        if tokens.is_file():
            return tokens.stat().st_size > 0

        return tokens.is_dir() and any(tokens.iterdir())

    def _remember_token_secrets(self) -> None:
        """A session token grants account access; it must never reach a log line, wherever it turns up."""
        for value in _token_values(self._config.garmin_tokens):
            self._secrets.add(value)


def _token_values(tokens: Path) -> Iterator[str]:
    files = sorted(tokens.glob("*")) if tokens.is_dir() else [tokens]
    for file in files:
        try:
            yield from _strings_in(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            # An unreadable token store is the login command's problem, not the redactor's.
            continue


def _strings_in(payload: Any) -> Iterator[str]:
    if isinstance(payload, str):
        yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _strings_in(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _strings_in(value)
