import json
import logging
from pathlib import Path

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from dreeve_garmin_connector.auth import AuthenticationRequired, Authenticator
from dreeve_garmin_connector.cli import EXIT_FAILED, EXIT_OK, MfaPromptUnavailable, login, main
from dreeve_garmin_connector.config import Config, InvalidConfiguration
from dreeve_garmin_connector.garmin import AuthenticationFailed, RateLimited
from dreeve_garmin_connector.logging_ import REDACTED, Secrets
from tests.stubs import StubSessionFactory

EMAIL = "rider@example.com"
PASSWORD = "sup3r-s3cret"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.a-refresh-token-that-grants-account-access"


def config_for(tmp_path: Path, **overrides: str) -> Config:
    return Config.from_env(
        {
            "GARMIN_EMAIL": EMAIL,
            "GARMIN_PASSWORD": PASSWORD,
            "GARMINTOKENS": str(tmp_path / "tokens"),
            **overrides,
        }
    )


def store_session(tmp_path: Path, token: str = TOKEN) -> Path:
    tokens = tmp_path / "tokens"
    tokens.mkdir(exist_ok=True)
    # Nested the way real token stores are, so the redactor has to walk the whole document.
    stored = {
        "refresh_token": token,
        "scopes": ["CONNECT_READ"],
        "oauth": {"tokens": [{"value": token}]},
        "expires_at": 1767225600,
    }
    (tokens / "token.json").write_text(json.dumps(stored), encoding="utf-8")

    return tokens


def test_it_resumes_a_stored_session_without_handing_over_the_password(tmp_path: Path) -> None:
    store_session(tmp_path)
    factory = StubSessionFactory()

    Authenticator(config_for(tmp_path), Secrets(), factory).resume()

    # Withholding the password is the enforcement: the library falls back to a credential login on
    # its own whenever the token store fails to load.
    assert factory.sessions[0].constructed_with["password"] is None
    assert factory.sessions[0].constructed_with["email"] == EMAIL
    assert factory.sessions[0].logins == [str(tmp_path / "tokens")]


def test_it_asks_for_a_human_when_there_is_no_session_and_never_touches_garmin(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    factory = StubSessionFactory()

    with caplog.at_level(logging.INFO), pytest.raises(AuthenticationRequired) as raised:
        Authenticator(config_for(tmp_path), Secrets(), factory).resume()

    assert "docker compose run --rm connector login" in str(raised.value)
    assert "rate-limited" in str(raised.value)
    # Nothing was constructed, so no request was made and no failed login was counted against us.
    assert factory.sessions == []


def test_a_missing_session_is_not_a_reason_to_try_the_password(tmp_path: Path) -> None:
    factory = StubSessionFactory()
    authenticator = Authenticator(config_for(tmp_path, ALLOW_PASSWORD_LOGIN="false"), Secrets(), factory)

    for _ in range(3):
        with pytest.raises(AuthenticationRequired):
            authenticator.resume()

    assert factory.sessions == []


def test_password_login_is_permitted_exactly_once_per_process(tmp_path: Path) -> None:
    factory = StubSessionFactory()
    authenticator = Authenticator(config_for(tmp_path, ALLOW_PASSWORD_LOGIN="true"), Secrets(), factory)

    authenticator.resume()

    with pytest.raises(AuthenticationRequired):
        authenticator.resume()

    assert len(factory.sessions) == 1
    assert factory.sessions[0].constructed_with["password"] == PASSWORD


def test_a_rejected_password_is_never_tried_a_second_time(tmp_path: Path) -> None:
    # The retry storm this prevents is precisely what gets an account blocked.
    factory = StubSessionFactory(login_error=GarminConnectAuthenticationError("rejected"))
    authenticator = Authenticator(config_for(tmp_path, ALLOW_PASSWORD_LOGIN="true"), Secrets(), factory)

    with pytest.raises(AuthenticationFailed):
        authenticator.resume()

    with pytest.raises(AuthenticationRequired):
        authenticator.resume()

    assert len(factory.sessions) == 1


def test_a_stored_session_is_preferred_over_the_password_it_is_allowed_to_use(tmp_path: Path) -> None:
    store_session(tmp_path)
    factory = StubSessionFactory()

    Authenticator(config_for(tmp_path, ALLOW_PASSWORD_LOGIN="true"), Secrets(), factory).resume()

    assert factory.sessions[0].constructed_with["password"] is None


def test_an_empty_token_store_counts_as_no_session(tmp_path: Path) -> None:
    (tmp_path / "tokens").mkdir()
    factory = StubSessionFactory()

    with pytest.raises(AuthenticationRequired):
        Authenticator(config_for(tmp_path), Secrets(), factory).resume()


def test_a_token_store_that_is_a_single_file_is_understood_too(tmp_path: Path) -> None:
    (tmp_path / "tokens").write_text(json.dumps({"refresh_token": TOKEN}), encoding="utf-8")
    factory = StubSessionFactory()

    Authenticator(config_for(tmp_path), Secrets(), factory).resume()

    assert factory.sessions[0].constructed_with["password"] is None


def test_it_learns_the_stored_tokens_so_they_can_never_be_logged(tmp_path: Path) -> None:
    store_session(tmp_path)
    secrets = Secrets()

    Authenticator(config_for(tmp_path), secrets, StubSessionFactory()).resume()

    assert secrets.redact(f"resumed with {TOKEN}") == f"resumed with {REDACTED}"


def test_an_unreadable_token_store_is_not_the_redactors_problem(tmp_path: Path) -> None:
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    (tokens / "token.json").write_text("{ truncated", encoding="utf-8")

    Authenticator(config_for(tmp_path), Secrets(), StubSessionFactory()).resume()


def test_a_rate_limited_resume_is_reported_as_such(tmp_path: Path) -> None:
    store_session(tmp_path)
    factory = StubSessionFactory(login_error=GarminConnectTooManyRequestsError("429"))

    with pytest.raises(RateLimited):
        Authenticator(config_for(tmp_path), Secrets(), factory).resume()


def test_logging_in_hands_over_the_password_and_a_way_to_ask_for_a_code(tmp_path: Path) -> None:
    factory = StubSessionFactory()
    prompt = "123456"

    Authenticator(config_for(tmp_path), Secrets(), factory).log_in(prompt_mfa=lambda: prompt)

    session = factory.sessions[0]
    assert session.constructed_with["password"] == PASSWORD
    assert session.constructed_with["prompt_mfa"]() == prompt
    assert session.logins == [str(tmp_path / "tokens")]


def test_logging_in_needs_a_password(tmp_path: Path) -> None:
    config = Config.from_env({"GARMIN_EMAIL": EMAIL, "GARMINTOKENS": str(tmp_path / "tokens")})

    with pytest.raises(InvalidConfiguration) as raised:
        Authenticator(config, Secrets(), StubSessionFactory()).log_in(prompt_mfa=lambda: "123456")

    assert "required to log in" in str(raised.value)


def test_the_login_command_reports_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("dreeve_garmin_connector.cli.Authenticator", _AuthenticatorThatSucceeds)

    assert login(config_for(tmp_path), Secrets()) == EXIT_OK


def test_the_login_command_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("dreeve_garmin_connector.cli.Authenticator", _AuthenticatorThatFails)

    with caplog.at_level(logging.ERROR):
        assert login(config_for(tmp_path), Secrets()) == EXIT_FAILED

    assert "Login failed" in caplog.text


def test_the_command_line_refuses_an_unusable_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("os.environ", {})

    assert main(["login"]) == EXIT_FAILED
    assert "GARMIN_EMAIL is required" in capsys.readouterr().err


def test_the_command_line_runs_the_login_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("os.environ", {"GARMIN_EMAIL": EMAIL, "GARMINTOKENS": str(tmp_path / "tokens")})
    monkeypatch.setattr("dreeve_garmin_connector.cli.Authenticator", _AuthenticatorThatSucceeds)

    assert main(["login"]) == EXIT_OK


def test_the_module_can_be_run_with_python_dash_m() -> None:
    # `python -m dreeve_garmin_connector login` is the documented way in; an import error here is fatal.
    import dreeve_garmin_connector.__main__ as entry_point

    assert entry_point.main is main


def test_it_says_where_to_type_the_code_when_there_is_no_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from dreeve_garmin_connector.cli import ask_for_mfa_code

    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(MfaPromptUnavailable) as raised:
        ask_for_mfa_code()

    assert "docker compose run --rm connector login" in str(raised.value)


def test_it_asks_for_the_code_when_there_is_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from dreeve_garmin_connector.cli import ask_for_mfa_code

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "  123456 ")

    assert ask_for_mfa_code() == "123456"


class _AuthenticatorThatSucceeds:
    def __init__(self, config: Config, secrets: Secrets) -> None:
        self._config = config
        self._secrets = secrets

    def log_in(self, prompt_mfa: object) -> None:
        self.prompted_with = prompt_mfa


class _AuthenticatorThatFails:
    def __init__(self, config: Config, secrets: Secrets) -> None:
        self._config = config
        self._secrets = secrets

    def log_in(self, prompt_mfa: object) -> None:
        self.prompted_with = prompt_mfa
        raise AuthenticationFailed("Garmin rejected the credentials")
