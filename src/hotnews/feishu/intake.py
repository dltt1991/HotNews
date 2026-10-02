"""Durably accept long-connection events before acknowledging them."""

import json
from typing import Mapping

from ..domain import ValidationError
from ..storage.database import Database
from ..storage.events import EventRepository
from ..storage.outbox import OutboxRepository
from .events import normalize_event


ACKNOWLEDGEMENT = "已收到，将在 5 分钟内处理。"


class EventIntake:
    """Filter and atomically queue one SDK event and its fixed acknowledgement."""

    def __init__(self, database: Database, bot_open_id: str):
        if not isinstance(bot_open_id, str) or not bot_open_id.strip():
            raise ValidationError("event intake requires a resolved Feishu bot open ID")
        self.database = database
        self.bot_open_id = bot_open_id
        self.events = EventRepository(database)
        self.outbox = OutboxRepository(database)

    def handle(self, payload: Mapping[str, object]) -> bool:
        try:
            event = normalize_event(payload, self.bot_open_id)
            if event is None:
                return True
            message = payload["event"]["message"]
            content = json.loads(message["content"])
            mentions = message.get("mentions", [])
            # Validate untrusted archives before opening the storage transaction.
            encoded_mentions = json.dumps(mentions, ensure_ascii=True, sort_keys=True,
                                          allow_nan=False, separators=(",", ":"))
            if json.loads(encoded_mentions) != mentions:
                raise ValidationError("message mentions do not round-trip")
            values = dict(vars(event), raw_text=content["text"], mentions=mentions)
        except (KeyError, TypeError, ValueError, UnicodeError, RecursionError, ValidationError):
            # Malformed and irrelevant remote events are terminally ignored.
            return True
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self.events.insert(values, connection=connection):
                self.outbox.enqueue(event.chat_id, "text", {"text": ACKNOWLEDGEMENT},
                                    "event:%s:ack" % event.event_id, connection=connection)
        return True
