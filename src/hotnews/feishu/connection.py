"""Supervise Feishu long-connection state without exposing network secrets."""

from dataclasses import dataclass
from datetime import datetime, timezone
import threading
from typing import Callable, Optional

from ..config import FeishuConfig
from .proxy import redact_sensitive


class FatalConnectionError(RuntimeError):
    """A non-retryable long-connection failure with a sanitized message."""

    def __init__(self, message: object):
        super().__init__(redact_sensitive(message))


class SDKCompatibilityError(FatalConnectionError):
    """The installed SDK no longer matches the isolated adapter."""


@dataclass(frozen=True)
class ConnectionSnapshot:
    state: str
    connected_at: Optional[datetime]
    last_event_at: Optional[datetime]
    reconnect_attempts: int
    last_error: Optional[str]


class ConnectionStatus:
    """A small thread-safe, non-persistent operational snapshot."""

    def __init__(self, clock: Callable[[], datetime] = None):
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._state = "starting"
        self._connected_at = None
        self._last_event_at = None
        self._reconnect_attempts = 0
        self._last_error = None

    def snapshot(self) -> ConnectionSnapshot:
        with self._lock:
            return ConnectionSnapshot(self._state, self._connected_at, self._last_event_at,
                                      self._reconnect_attempts, self._last_error)

    def starting(self) -> None:
        with self._lock:
            self._state = "starting"

    def connected(self) -> None:
        with self._lock:
            self._state = "connected"
            self._connected_at = self._clock()
            self._reconnect_attempts = 0
            self._last_error = None

    def event_received(self) -> None:
        with self._lock:
            self._last_event_at = self._clock()

    def reconnecting(self, error: object) -> None:
        with self._lock:
            self._state = "reconnecting"
            self._reconnect_attempts += 1
            self._last_error = redact_sensitive(error)

    def stopped(self) -> None:
        with self._lock:
            self._state = "stopped"

    def fatal(self, error: object) -> None:
        with self._lock:
            self._state = "fatal"
            self._last_error = redact_sensitive(error)


class FeishuLongConnection:
    """Retry initial transport failures and delegate an established session to the SDK."""

    def __init__(self, config: FeishuConfig, intake, status: ConnectionStatus,
                 connector_factory=None, wait=None, jitter=None):
        if connector_factory is None:
            from .ws_compat import build_sdk_connector
            connector_factory = build_sdk_connector
        self.config = config
        self.intake = intake
        self.status = status
        self.connector_factory = connector_factory
        self.wait = wait or (lambda stop, delay: stop.wait(delay))
        self.jitter = jitter or __import__("random").random

    def _receive(self, payload) -> None:
        acknowledged = self.intake.handle(payload)
        if acknowledged:
            self.status.event_received()

    def run(self, stop_event: threading.Event) -> None:
        attempt = 0
        self.status.starting()
        while not stop_event.is_set():
            connector = None
            try:
                connector = self.connector_factory(self.config, self._receive)
                connector.run(stop_event, self.status.connected,
                              lambda error=None: self.status.reconnecting(error or "connection lost"))
                if not stop_event.is_set():
                    raise OSError("Feishu connection stopped unexpectedly")
            except FatalConnectionError as error:
                safe = FatalConnectionError(error)
                self.status.fatal(safe)
                raise safe from None
            except Exception as error:
                if stop_event.is_set():
                    break
                attempt += 1
                self.status.reconnecting(error)
                delay = min(60.0, float(2 ** min(attempt - 1, 6)))
                delay += min(1.0, max(0.0, float(self.jitter())))
                if self.wait(stop_event, delay):
                    break
            finally:
                if connector is not None:
                    connector.close()
        if self.status.snapshot().state != "fatal":
            self.status.stopped()
