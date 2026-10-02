"""Authenticated Feishu v2 callbacks and structural current-bot mention filtering."""

import hmac
import json
import re
from datetime import datetime, timezone
from typing import Mapping, Optional

from ..config import FeishuConfig
from ..domain import NormalizedEvent, ValidationError
from .crypto import decrypt_payload, verify_signature


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    return value


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a nonempty string" % name)
    return value


def decode_callback(payload: dict, config: FeishuConfig) -> dict:
    """Decrypt when needed and check the verification token, including challenges.

    HTTP callers must use decode_request first to also authenticate request headers.
    """
    payload = _object(payload, "callback")
    if "encrypt" in payload:
        encrypted = _text(payload["encrypt"], "encrypt")
        if not config.encrypt_key:
            raise PermissionError("Feishu callback encryption is not configured")
        payload = decrypt_payload(encrypted, config.encrypt_key)
    if payload.get("type") == "url_verification":
        supplied = payload.get("token")
    else:
        header = _object(payload.get("header"), "callback header")
        supplied = header.get("token")
    expected = config.verification_token
    if not isinstance(supplied, str) or not isinstance(expected, str) or not expected:
        raise PermissionError("invalid Feishu verification token")
    try:
        matches = hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        raise PermissionError("invalid Feishu verification token") from None
    if not matches:
        raise PermissionError("invalid Feishu verification token")
    if payload.get("type") != "url_verification":
        if payload["header"].get("app_id") != config.app_id:
            raise PermissionError("invalid Feishu callback application")
    return payload


def decode_request(raw_body: bytes, headers: Mapping[str, str], config: FeishuConfig) -> dict:
    """Parse and authenticate HTTP callbacks, preserving the exact signed body.

    Feishu URL verification checks the token but omits the signature in the official
    SDK. Every other callback requires a signature when Encrypt Key is configured.
    """
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError("invalid Feishu callback JSON") from None
    decoded = decode_callback(payload, config)
    if config.encrypt_key and decoded.get("type") != "url_verification":
        verify_signature(raw_body, headers, config.encrypt_key)
    return decoded


def url_verification_response(payload: dict, config: FeishuConfig) -> Optional[dict]:
    decoded = decode_callback(payload, config)
    if decoded.get("type") != "url_verification":
        return None
    return {"challenge": _text(decoded.get("challenge"), "challenge")}


def normalize_event(payload: dict, config, received_at: Optional[datetime] = None) -> Optional[NormalizedEvent]:
    """Accept only group text from a user with a structural mention of this bot.

    A FeishuConfig denotes a legacy authenticated callback. A string is the
    already-resolved bot identity used by the authenticated long connection.
    """
    if isinstance(config, FeishuConfig):
        payload = decode_callback(payload, config)
        bot_id = config.bot_open_id
    else:
        payload = _object(payload, "event payload")
        bot_id = config
    if payload.get("type") == "url_verification":
        return None
    if payload.get("schema") != "2.0":
        raise ValidationError("Feishu callback requires v2 schema")
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
