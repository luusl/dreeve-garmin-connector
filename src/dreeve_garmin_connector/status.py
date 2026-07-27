"""What the container says about itself, over HTTP and to Docker's healthcheck.

A dead token store is by far the likeliest real failure here, and it is silent: the container keeps
running, the logs go quiet, and nothing arrives in the watch folder. So authentication state is
reported first-class rather than left in a log line nobody reads.
"""

import json
import logging
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from dreeve_garmin_connector.ledger import ActivityStatus
from dreeve_garmin_connector.sync import CycleResult

HEARTBEAT_FILENAME = "heartbeat"
# Three intervals without a completed cycle: one missed cycle is noise, three is a problem.
UNHEALTHY_AFTER_CYCLES = 3
SHUTDOWN_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger(__name__)


class Status:
    """Written by the loop, read by the HTTP server on another thread."""

    def __init__(self, poll_interval: int, started_at: datetime) -> None:
        self._lock = threading.Lock()
        self._poll_interval = poll_interval
        self._started_at = started_at
        self._cycles = 0
        self._last_successful_sync: datetime | None = None
        self._last_cycle: CycleResult | None = None
        self._next_run_at: datetime | None = None
        self._backoff_seconds = 0.0
        self._auth_error: str | None = None
        self._last_error: str | None = None
        self._counts: dict[ActivityStatus, int] = {}

    def cycle_succeeded(self, result: CycleResult, counts: dict[ActivityStatus, int], at: datetime) -> None:
        with self._lock:
            self._cycles += 1
            self._last_successful_sync = at
            self._last_cycle = result
            self._counts = counts
            self._backoff_seconds = 0.0
            self._auth_error = None
            self._last_error = None

    def cycle_failed(self, message: str) -> None:
        with self._lock:
            self._cycles += 1
            self._last_error = message

    def rate_limited(self, backoff_seconds: float, message: str) -> None:
        with self._lock:
            self._cycles += 1
            self._backoff_seconds = backoff_seconds
            self._last_error = message

    def authentication_failed(self, message: str) -> None:
        with self._lock:
            self._auth_error = message

    def sleeping_until(self, at: datetime) -> None:
        with self._lock:
            self._next_run_at = at

    def is_healthy(self, now: datetime) -> bool:
        with self._lock:
            if self._auth_error is not None:
                return False

            # Before the first successful cycle, the container is given the same grace as after one.
            since = self._last_successful_sync or self._started_at

            return now - since <= timedelta(seconds=self._poll_interval * UNHEALTHY_AFTER_CYCLES)

    def to_dict(self, now: datetime) -> dict[str, Any]:
        healthy = self.is_healthy(now)
        with self._lock:
            return {
                "healthy": healthy,
                "startedAt": self._started_at.isoformat(),
                "cycles": self._cycles,
                "lastSuccessfulSync": _instant(self._last_successful_sync),
                "nextRunAt": _instant(self._next_run_at),
                "backoffSeconds": self._backoff_seconds,
                "authentication": self._auth_error or "ok",
                "lastError": self._last_error,
                "lastCycle": _cycle(self._last_cycle),
                "backlog": self._last_cycle.backlog if self._last_cycle else 0,
                "activities": {status.value: count for status, count in self._counts.items()},
            }


class StatusServer:
    """A stdlib HTTP server on a daemon thread. Not worth a dependency, and not worth a request loop."""

    def __init__(self, address: str, status: Status, clock: Any) -> None:
        self._status = status
        self._clock = clock
        self._host, _, port = address.rpartition(":")
        self._port = int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = _handler_for(self._status, self._clock)
        self._server = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="status", daemon=True)
        self._thread.start()
        logger.info("Serving /healthz and /status on %s:%d", self._host, self._port)

    @property
    def port(self) -> int:
        """The port actually bound, which differs from the configured one when that was 0."""
        return self._server.server_address[1] if self._server else self._port

    def stop(self) -> None:
        if self._server is None:
            return

        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)


def _handler_for(status: Status, clock: Any) -> type[BaseHTTPRequestHandler]:
    class StatusHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            now = clock.now()
            if self.path.startswith("/healthz"):
                healthy = status.is_healthy(now)
                self._respond(200 if healthy else 503, {"healthy": healthy})
            elif self.path.startswith("/status"):
                self._respond(200, status.to_dict(now))
            else:
                self._respond(404, {"error": "not found"})

        def _respond(self, code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("status server: " + format, *args)

    return StatusHandler


def _instant(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _cycle(result: CycleResult | None) -> dict[str, int] | None:
    if result is None:
        return None

    return {
        "listed": result.listed,
        "delivered": result.delivered,
        "failed": result.failed,
        "skipped": result.skipped,
        "withoutFile": result.without_file,
        "backlog": result.backlog,
    }
