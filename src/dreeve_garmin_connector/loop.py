"""The daemon: one cycle, then wait, forever.

Three things make this more than a `while True`. The wait is jittered, so every deployment of this
image does not hit Garmin on the same second of the hour. A rate limit backs the interval off
exponentially instead of retrying into the wall. And a session Garmin has *rejected* is never
retried at all — the container stays up, reports unhealthy and waits for a human, because hammering
a rejecting login endpoint is what turns a bad hour into a blocked account.
"""

import logging
import random
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import FrameType
from typing import Any

from dreeve_garmin_connector.auth import AuthenticationRequired
from dreeve_garmin_connector.config import Config
from dreeve_garmin_connector.delivery import UndeliverableFile
from dreeve_garmin_connector.garmin import AuthenticationFailed, GarminError, RateLimited
from dreeve_garmin_connector.ledger import CorruptLedger
from dreeve_garmin_connector.status import HEARTBEAT_FILENAME, Status
from dreeve_garmin_connector.sync import Clock, Sync

logger = logging.getLogger(__name__)


class SyncLoop:
    def __init__(
        self,
        config: Config,
        sync_factory: Callable[[Callable[[], bool]], Sync],
        status: Status,
        clock: Clock,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._config = config
        self._sync_factory = sync_factory
        self._status = status
        self._clock = clock
        self._stop = threading.Event()
        # Waiting on the stop event rather than sleeping means SIGTERM is acted on immediately
        # instead of an hour later.
        self._sleep = sleep or self._stop.wait
        self._sync: Sync | None = None
        self._session_rejected = False
        self._backoff_seconds = 0.0

    def request_stop(self, signum: int | None = None, frame: FrameType | None = None) -> None:  # noqa: ARG002
        """Signal handler. The cycle in flight finishes its current file and flushes the ledger."""
        if not self._stop.is_set():
            logger.info("Shutdown requested; finishing what is in flight")
        self._stop.set()

    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def run(self) -> None:
        cycles = 0
        while not self._stop.is_set():
            self._run_one_cycle()
            cycles += 1

            if self._config.max_cycles and cycles >= self._config.max_cycles:
                logger.info("Reached MAX_CYCLES (%d); stopping", self._config.max_cycles)
                break
            if self._stop.is_set():
                break

            delay = self.next_delay()
            self._status.sleeping_until(self._clock.now() + timedelta(seconds=delay))
            logger.info("Next cycle in %d seconds", round(delay))
            self._sleep(delay)

    def next_delay(self) -> float:
        """The interval, jittered — or the current backoff, which is deliberately not jittered."""
        if self._backoff_seconds:
            return self._backoff_seconds

        spread = self._config.poll_interval * self._config.poll_jitter_pct / 100
        return self._config.poll_interval + random.uniform(-spread, spread)

    def _run_one_cycle(self) -> None:
        sync = self._resolve_sync()
        if sync is None:
            return

        try:
            result = sync.run_once()
        except RateLimited as exception:
            self._back_off()
            logger.warning("Garmin is rate-limiting us; backing off for %d seconds", round(self._backoff_seconds))
            self._status.rate_limited(self._backoff_seconds, str(exception))
        except AuthenticationRequired as exception:
            self._no_session(exception)
        except AuthenticationFailed as exception:
            self._session_was_rejected(exception)
        except (GarminError, CorruptLedger, UndeliverableFile, OSError) as exception:
            logger.error("Cycle failed: %s", exception)
            self._status.cycle_failed(str(exception))
        else:
            self._backoff_seconds = 0.0
            self._status.cycle_succeeded(result, sync.ledger.counts_by_status(), self._clock.now())
            self._touch_heartbeat()
            logger.info("Cycle finished: %s", result)

    def _resolve_sync(self) -> Sync | None:
        """Built once and kept: rebuilding it would resume the session again on every cycle."""
        if self._sync is not None:
            return self._sync

        if self._session_rejected:
            return None

        try:
            # The cycle asks this between activities, so a SIGTERM lands within one file.
            self._sync = self._sync_factory(self._stop.is_set)
        except AuthenticationRequired as exception:
            # No session to resume, and no request was made looking for one. Cheap to try again
            # next cycle, which is how a `login` run while we wait is picked up without a restart.
            self._no_session(exception)
        except AuthenticationFailed as exception:
            self._session_was_rejected(exception)
        except RateLimited as exception:
            self._back_off()
            self._status.rate_limited(self._backoff_seconds, str(exception))
        except (GarminError, CorruptLedger, UndeliverableFile, OSError) as exception:
            logger.error("Could not start a sync: %s", exception)
            self._status.cycle_failed(str(exception))

        return self._sync

    def _no_session(self, exception: Exception) -> None:
        logger.warning("%s", exception)
        self._status.authentication_failed(str(exception))
        self._sync = None

    def _session_was_rejected(self, exception: Exception) -> None:
        # Deliberately terminal for the life of the process. Whatever we have, Garmin does not want
        # it, and asking again every hour is a retry storm against an endpoint that is saying no.
        self._session_rejected = True
        self._sync = None
        self._status.authentication_failed(str(exception))
        logger.error("Garmin rejected the session, and this connector will not try again: %s", exception)

    def _back_off(self) -> None:
        doubled = self._backoff_seconds * 2 if self._backoff_seconds else self._config.poll_interval * 2
        self._backoff_seconds = min(float(doubled), float(self._config.max_backoff_seconds))

    def _touch_heartbeat(self) -> None:
        """The healthcheck's fallback when HTTP_ADDR is off."""
        heartbeat = self._config.state_dir / HEARTBEAT_FILENAME
        try:
            heartbeat.parent.mkdir(parents=True, exist_ok=True)
            heartbeat.touch()
        except OSError as exception:
            logger.warning("Could not write the heartbeat file %s: %s", heartbeat, exception)


def heartbeat_path(state_dir: Path) -> Path:
    return state_dir / HEARTBEAT_FILENAME
