"""Normalize long-connection events with structural current-bot mention filtering."""

import json
import re
from datetime import datetime, timezone
from typing import Optional

from ..domain import NormalizedEvent, ValidationError


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a nonempty string" % name)
    return value


def normalize_event(payload: dict, bot_open_id: str,
                    received_at: Optional[datetime] = None) -> Optional[NormalizedEvent]:
    """Accept only group text from a user with a structural mention of this bot."""
    payload = _object(payload, "event payload")
    bot_id = bot_open_id
    if payload.get("schema") != "2.0":
        raise ValidationError("Feishu event requires v2 schema")
    header = payload["header"]
    if header.get("event_type") != "im.message.receive_v1":
        return None
    event = _object(payload.get("event"), "event")
    message = _object(event.get("message"), "message")
    sender = _object(event.get("sender"), "sender")
    if message.get("chat_type") != "group" or message.get("message_type") != "text":
        return None
    if sender.get("sender_type") != "user":
        return None
    sender_ids = _object(sender.get("sender_id"), "sender_id")
    sender_id = _text(sender_ids.get("open_id"), "sender open_id")
    if not isinstance(bot_id, str) or not bot_id.strip() or sender_id == bot_id:
        return None
    mentions = message.get("mentions", [])
    if not isinstance(mentions, list):
        raise ValidationError("message mentions must be an array")
    keys = []
    for mention in mentions:
        mention = _object(mention, "mention")
        identity = _object(mention.get("id"), "mention id")
        if identity.get("open_id") == bot_id:
            key = _text(mention.get("key"), "mention key")
            if not re.fullmatch(r"@_user_\d+", key):
                raise ValidationError("invalid Feishu mention key")
            keys.append(key)
    if not keys:
        return None
    content = message.get("content")
    if not isinstance(content, str):
        raise ValidationError("message content must be a JSON string")
    try:
        content = json.loads(content)
    except (ValueError, RecursionError):
        raise ValidationError("invalid Feishu message content JSON") from None
    content = _object(content, "message content")
    text = content.get("text")
    if not isinstance(text, str):
        raise ValidationError("message text must be a string")
    pattern = "(?:%s)(?!\\d)" % "|".join(re.escape(key) for key in keys)
    text = re.sub(pattern, "", text).strip()
    if not text:
        return None
    return NormalizedEvent(
        event_id=_text(header.get("event_id"), "event_id"),
        message_id=_text(message.get("message_id"), "message_id"),
        chat_id=_text(message.get("chat_id"), "chat_id"),
        sender_id=sender_id, text=text,
        received_at=received_at if received_at is not None else datetime.now(timezone.utc),
    )
