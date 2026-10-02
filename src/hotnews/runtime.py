"""Own the three local services and Feishu sends; Codex owns research only."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import os
from queue import Queue
import threading
import time
from uuid import uuid4

from .admin.server import serve_admin
from .config import AppConfig, load_feishu_config
from .domain import ValidationError
from .feishu.client import FeishuAPIError, FeishuClient
from .feishu.connection import ConnectionStatus, FeishuLongConnection
from .feishu.intake import EventIntake
from .storage.database import Database
from .storage.outbox import OutboxRepository


logger = logging.getLogger(__name__)


class RuntimeServiceError(RuntimeError):
    """A service stopped unexpectedly; its peer services have been stopped."""


class OutboxWorker:
    def __init__(self, config: AppConfig, client: FeishuClient, clock=time.monotonic):
        self.config = config
        self.client = client
        self.outbox = OutboxRepository(Database(config.database_path))
        self.owner = "outbox:" + str(uuid4())
        self.clock = clock

    def retry_delay(self, attempt: int) -> int:
        """Compute capped exponential backoff without constructing huge integers."""
        delay = self.config.worker.outbox_backoff_seconds
        maximum = self.config.worker.outbox_backoff_max_seconds
        for _ in range(max(0, attempt - 1)):
            if delay >= maximum:
                break
            delay *= 2
        return min(delay, maximum)

    def _send(self, item):
        if item.kind == "text":
            if set(item.content) != {"text"} or not isinstance(item.content["text"], str):
                raise ValidationError("invalid text payload")
            payload = self.client.send_text(item.chat_id, item.content["text"], item.idempotency_key)
        elif item.kind == "card":
            payload = self.client.send_card(item.chat_id, dict(item.content), item.idempotency_key)
        else:
            raise ValidationError("unsupported outbox kind")
        data = payload.get("data")
        message_id = data.get("message_id") if isinstance(data, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            # Delivery might have happened. Retry with the same UUID until an
            # explicit message confirmation can be recorded, never assume sent.
            raise FeishuAPIError("Feishu message confirmation is missing", retryable=True)
        return message_id

    def run_once(self, now: datetime, stop_event=None) -> int:
        """Send a bounded batch, claiming each row immediately before its send."""
        processed = 0
        started = self.clock()

        def current_time():
            return now + timedelta(seconds=max(0, self.clock() - started))

        while processed < self.config.worker.max_queued_events:
            if stop_event is not None and stop_event.is_set():
                break
            items = self.outbox.claim(self.owner, 1, current_time(), self.config.worker.lease_seconds)
            if not items:
                break
            item = items[0]
            processed += 1
            if item.attempts > self.config.worker.outbox_max_attempts:
                self.outbox.fail(item.id, self.owner, "delivery attempts exhausted", current_time())
                continue
            try:
                message_id = self._send(item)
            except ValidationError:
                self.outbox.fail(item.id, self.owner, "invalid delivery payload", current_time())
                logger.error("Outbox item %s rejected: invalid payload", item.id)
            except FeishuAPIError as error:
                # Do not persist remote messages, request bodies or credentials.
                reason = "Feishu delivery failed"
                if not error.retryable or item.attempts >= self.config.worker.outbox_max_attempts:
                    self.outbox.fail(item.id, self.owner, reason, current_time())
                    logger.error("Outbox item %s failed permanently", item.id)
                else:
                    delay = self.retry_delay(item.attempts)
                    if error.retry_after is not None:
                        delay = max(delay, error.retry_after)
                    try:
                        retry_at = current_time() + timedelta(seconds=delay)
                    except OverflowError:
                        # A remote finite delay can exceed datetime's range.
                        # Keep this row deferred rather than crashing all sends.
                        retry_at = datetime.max.replace(tzinfo=timezone.utc)
                    if self.outbox.retry(item.id, self.owner, reason, retry_at, now=current_time()):
                        logger.warning("Outbox item %s deferred for retry", item.id)
                    else:
                        logger.info("Outbox item %s closed after subscription cancellation", item.id)
            else:
                # A local persistence failure propagates to the owning runtime.
                # The lease expires for recovery using the unchanged send UUID.
                self.outbox.sent(item.id, self.owner, message_id, current_time())
        return processed

    def run(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            self.run_once(datetime.now(timezone.utc), stop_event)
            stop_event.wait(0.5)


def run_service(config: AppConfig, stop_event: threading.Event) -> None:
    """Migrate, resolve bot identity, then run connection/admin/outbox together."""
    if config.admin.host != "127.0.0.1":
        raise ValidationError("admin must bind to 127.0.0.1")
    if stop_event.is_set():
        return
    Database(config.database_path).migrate()
    feishu_config = load_feishu_config(os.environ)
    client = FeishuClient(feishu_config)
    feishu_config = replace(feishu_config, bot_open_id=client.get_bot_open_id())
    worker = OutboxWorker(config, client)
    status = ConnectionStatus()
    connection = FeishuLongConnection(
        feishu_config, EventIntake(Database(config.database_path), feishu_config.bot_open_id), status)
    failures = Queue()

    def supervise(name, target, *args):
        try:
            target(*args)
            if not stop_event.is_set():
                raise RuntimeServiceError("service exited unexpectedly")
        except Exception:
            # Thread exception hooks print exception messages/tracebacks. Handle
            # them here, reporting only a fixed service name to the operator.
            failures.put(name)
            logger.error("%s service failed; stopping runtime", name)
            stop_event.set()

    threads = [
        threading.Thread(name="hotnews-connection", target=supervise,
                         args=("connection", connection.run, stop_event)),
        threading.Thread(name="hotnews-admin", target=supervise,
                         args=("admin", serve_admin, config, stop_event, status.snapshot)),
        threading.Thread(name="hotnews-outbox", target=supervise,
                         args=("outbox", worker.run, stop_event)),
    ]
    started = []
    try:
        for thread in threads:
            thread.start()
            started.append(thread)
        stop_event.wait()
    finally:
        stop_event.set()
        for thread in started:
            thread.join()
    if not failures.empty():
        raise RuntimeServiceError("%s service failed" % failures.get()) from None
