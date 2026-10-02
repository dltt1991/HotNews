"""Strict JSON contracts for model-produced subscription commands."""

import json

from ..domain import Intent, Schedule, ValidationError


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValidationError("duplicate JSON fields")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValidationError("non-finite JSON numbers")


def parse_json(raw):
    """Decode only strict JSON, for stdin and persisted result snapshots alike."""
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValidationError("invalid JSON input") from None


def object_fields(value, required, optional=()):
    """Validate keys without reflecting untrusted values in diagnostics."""
    if not isinstance(value, dict):
        raise ValidationError("input must be a JSON object")
    if not set(required).issubset(value) or set(value) - set(required) - set(optional):
        raise ValidationError("missing or unsupported fields")
    return value


def nonempty_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string" % name)
    return value


def positive_integer(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError("%s must be a positive integer" % name)
    return value


def _strings(value, name):
    if not isinstance(value, list):
        raise ValidationError("%s must be a JSON array" % name)
    return tuple(nonempty_string(item, name) for item in value)


def parse_schedule(value):
    object_fields(value, ("kind",), ("daily_at", "interval_minutes"))
    if value["kind"] == "daily":
        object_fields(value, ("kind", "daily_at"))
        return Schedule("daily", daily_at=value["daily_at"])
    if value["kind"] == "interval":
        object_fields(value, ("kind", "interval_minutes"))
        return Schedule("interval", interval_minutes=value["interval_minutes"])
    raise ValidationError("subscription schedule must be daily or interval")


def parse_intent(value: dict) -> Intent:
    object_fields(value, ("action",), ("topic", "keywords", "search_terms", "schedule", "subscription_number"))
    action = nonempty_string(value["action"], "action")
    if action == "create_subscription":
        object_fields(value, ("action", "topic", "keywords"), ("search_terms", "schedule"))
        schedule = parse_schedule(value["schedule"]) if "schedule" in value else Schedule("daily", daily_at="09:00")
        return Intent(action, topic=nonempty_string(value["topic"], "topic"),
                      keywords=_strings(value["keywords"], "keywords"),
                      search_terms=_strings(value.get("search_terms", []), "search_terms"), schedule=schedule)
    if action in ("cancel_subscription", "run_subscription_now"):
        object_fields(value, ("action", "subscription_number"))
        return Intent(action, subscription_number=positive_integer(value["subscription_number"], "subscription_number"))
    if action in ("list_subscriptions", "show_help", "clarification_required"):
        object_fields(value, ("action",))
        return Intent(action)
    raise ValidationError("unsupported intent action")
