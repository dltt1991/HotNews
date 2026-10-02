from datetime import datetime, timezone
import threading
import unittest

from hotnews.config import FeishuConfig
from hotnews.feishu.connection import (ConnectionStatus, FatalConnectionError,
                                       FeishuLongConnection)


class Intake:
    def __init__(self, error=None):
        self.payloads = []
        self.error = error

    def handle(self, payload):
        if self.error:
            raise self.error
        self.payloads.append(payload)
        return True


class Connector:
    def __init__(self, action):
        self.action = action
        self.closed = False

    def run(self, stop_event, on_connected, on_reconnecting):
        return self.action(stop_event, on_connected, on_reconnecting)

    def close(self):
        self.closed = True


class FeishuConnectionTests(unittest.TestCase):
    def config(self):
        return FeishuConfig("app", "secret", bot_open_id="ou_bot")

    def test_status_transitions_are_thread_safe_and_errors_are_sanitized(self):
        clock_values = iter((datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc),
                             datetime(2026, 10, 2, 1, 1, tzinfo=timezone.utc)))
        status = ConnectionStatus(clock=lambda: next(clock_values))
        status.connected()
        status.event_received()
        status.reconnecting(RuntimeError(
            "lost wss://open.feishu.cn/ws?ticket=secret via http://u:p@proxy:7890"))
        snapshot = status.snapshot()
        self.assertEqual((snapshot.state, snapshot.reconnect_attempts), ("reconnecting", 1))
        self.assertEqual(snapshot.connected_at,
                         datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc))
        self.assertEqual(snapshot.last_event_at,
                         datetime(2026, 10, 2, 1, 1, tzinfo=timezone.utc))
        self.assertNotIn("secret", snapshot.last_error)
        self.assertNotIn("u:p", snapshot.last_error)

    def test_transient_initial_failure_retries_then_stops_cleanly(self):
        attempts = []
        waits = []

        def factory(config, callback):
            number = len(attempts) + 1
            attempts.append(number)
            if number == 1:
                return Connector(lambda *_: (_ for _ in ()).throw(OSError("offline")))

            def connected(stop_event, on_connected, _):
                on_connected()
                callback({"event": number})
                stop_event.set()
            return Connector(connected)

        status = ConnectionStatus()
        service = FeishuLongConnection(self.config(), Intake(), status, connector_factory=factory,
                                       wait=lambda stop, delay: waits.append(delay) or False,
                                       jitter=lambda: 0.0)
        service.run(threading.Event())
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(waits, [1.0])
        self.assertEqual(status.snapshot().state, "stopped")
        self.assertIsNotNone(status.snapshot().last_event_at)

    def test_fatal_failure_sets_safe_state_and_propagates(self):
        def factory(config, callback):
            return Connector(lambda *_: (_ for _ in ()).throw(
                FatalConnectionError("bad wss://host/ws?ticket=secret")))

        status = ConnectionStatus()
        service = FeishuLongConnection(self.config(), Intake(), status,
                                       connector_factory=factory)
        with self.assertRaises(FatalConnectionError) as caught:
            service.run(threading.Event())
        self.assertEqual(status.snapshot().state, "fatal")
        self.assertNotIn("secret", status.snapshot().last_error)
        self.assertNotIn("secret", str(caught.exception))

    def test_stop_during_backoff_prevents_another_attempt(self):
        stop = threading.Event()
        attempts = []

        def factory(config, callback):
            attempts.append(1)
            return Connector(lambda *_: (_ for _ in ()).throw(OSError("offline")))

        def wait(event, delay):
            event.set()
            return True

        service = FeishuLongConnection(self.config(), Intake(), ConnectionStatus(),
                                       connector_factory=factory, wait=wait)
        service.run(stop)
        self.assertEqual(attempts, [1])

    def test_check_returns_after_first_connection_and_stops_session(self):
        def factory(config, callback):
            def connected(stop_event, on_connected, _):
                on_connected()
                stop_event.wait(1)
            return Connector(connected)

        service = FeishuLongConnection(self.config(), Intake(), ConnectionStatus(),
                                       connector_factory=factory)
        snapshot = service.check(1)
        self.assertEqual(snapshot.state, "connected")

    def test_check_timeout_is_bounded_and_sanitized(self):
        gate = threading.Event()
        def factory(config, callback):
            return Connector(lambda *_: gate.wait(1))
        service = FeishuLongConnection(self.config(), Intake(), ConnectionStatus(),
                                       connector_factory=factory)
        try:
            with self.assertRaises(OSError) as caught:
                service.check(0.01)
            self.assertEqual(str(caught.exception), "Feishu connection check timed out")
        finally:
            gate.set()


if __name__ == "__main__":
    unittest.main()
