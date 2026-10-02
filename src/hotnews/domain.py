"""Immutable domain contracts shared by the hot-news services."""

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Mapping, Optional, Tuple
from urllib.parse import urlparse


class ValidationError(ValueError):
    """Raised when configuration or domain input violates its contract."""


class LeaseConflict(RuntimeError):
    """Raised when a work lease cannot be acquired or renewed."""


class VersionConflict(RuntimeError):
    """Raised when an optimistic update uses a stale version."""


def _require_non_empty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string" % name)


@dataclass(frozen=True)
class Schedule:
    kind: str
    daily_at: Optional[str] = None
    interval_minutes: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind == "daily":
            if not isinstance(self.daily_at, str) or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", self.daily_at
            ):
                raise ValidationError("daily schedule requires HH:MM")
            if self.interval_minutes is not None:
                raise ValidationError("daily schedule cannot include interval_minutes")
        elif self.kind == "interval":
            if not isinstance(self.interval_minutes, int) or isinstance(self.interval_minutes, bool):
                raise ValidationError("interval schedule requires interval_minutes")
            if self.interval_minutes < 5:
                raise ValidationError("interval must be at least five minutes")
            if self.daily_at is not None:
                raise ValidationError("interval schedule cannot include daily_at")
        elif self.kind == "manual":
            if self.daily_at is not None or self.interval_minutes is not None:
                raise ValidationError("manual schedule has no parameters")
        else:
            raise ValidationError("unsupported schedule kind")


@dataclass(frozen=True)
class Intent:
    action: str
    topic: Optional[str] = None
    keywords: Tuple[str, ...] = ()
    search_terms: Tuple[str, ...] = ()
    schedule: Optional[Schedule] = None
    subscription_number: Optional[int] = None

    def __post_init__(self) -> None:
        if self.action not in {
            "create_subscription", "list_subscriptions", "cancel_subscription",
            "run_subscription_now", "show_help", "clarification_required",
        }:
            raise ValidationError("unsupported intent action")
        if self.topic is not None:
            _require_non_empty(self.topic, "topic")
            if len(self.topic) > 200:
                raise ValidationError("topic must be at most 200 characters")
        if not isinstance(self.keywords, tuple):
            raise ValidationError("keywords must be a tuple")
        if len(self.keywords) > 20 or (self.action == "create_subscription" and not self.keywords):
            raise ValidationError("a subscription requires 1 to 20 keywords")
        for keyword in self.keywords:
            _require_non_empty(keyword, "keyword")
            if len(keyword) > 80:
                raise ValidationError("each keyword must be at most 80 characters")
        if not isinstance(self.search_terms, tuple):
            raise ValidationError("search_terms must be a tuple")
        for term in self.search_terms:
            _require_non_empty(term, "search term")
        if self.action == "create_subscription" and self.topic is None:
            raise ValidationError("create_subscription requires a topic")
        if self.subscription_number is not None and (
            not isinstance(self.subscription_number, int)
            or isinstance(self.subscription_number, bool)
            or self.subscription_number < 1
        ):
            raise ValidationError("subscription_number must be a positive integer")


@dataclass(frozen=True)
class NewsResult:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str
    event_key: str
    references: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("title", "source", "summary", "event_key"):
            _require_non_empty(getattr(self, name), name)
        if not isinstance(self.published_at, datetime):
            raise ValidationError("published_at must be a datetime")
        parsed = urlparse(self.url if isinstance(self.url, str) else "")
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValidationError("url must be an HTTP URL")
        if not isinstance(self.references, tuple):
            raise ValidationError("references must be a tuple")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class NormalizedEvent:
    event_id: str
    message_id: str
    chat_id: str
    sender_id: str
    text: str
    received_at: datetime


@dataclass(frozen=True)
class InboundEvent:
    id: str
    event_id: str
    message_id: str
    chat_id: str
    sender_id: str
    text: str
    received_at: datetime
    status: str
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    attempts: int = 0
    last_error: Optional[str] = None


@dataclass(frozen=True)
class Subscription:
    id: str
    chat_id: str
    display_number: int
    creator_id: str
    topic: str
    keywords: Tuple[str, ...]
    search_terms: Tuple[str, ...]
    schedule: Schedule
    state: str
    next_run_at: Optional[datetime]
    version: int
    created_at: datetime
    updated_at: datetime
    cancelled_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    consecutive_failures: int = 0
    alerted: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.topic, "topic")
        if len(self.topic) > 200:
            raise ValidationError("topic must be at most 200 characters")
        if not isinstance(self.keywords, tuple) or not 1 <= len(self.keywords) <= 20:
            raise ValidationError("a subscription requires 1 to 20 keywords")
        for keyword in self.keywords:
            _require_non_empty(keyword, "keyword")
            if len(keyword) > 80:
                raise ValidationError("each keyword must be at most 80 characters")
        if not isinstance(self.search_terms, tuple):
            raise ValidationError("search_terms must be a tuple")


@dataclass(frozen=True)
class SubscriptionRun:
    id: str
    subscription_id: str
    trigger: str
    status: str
    created_at: datetime
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    search_window_days: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class OutboxItem:
    id: str
    chat_id: str
    kind: str
    content: Mapping[str, object]
    idempotency_key: str
    status: str
    attempts: int
    created_at: datetime
    lease_owner: Optional[str] = None
    lease_until: Optional[datetime] = None
    last_error: Optional[str] = None


@dataclass(frozen=True)
class CommandResult:
    message: str
    subscription: Optional[Subscription] = None
    subscriptions: Tuple[Subscription, ...] = ()
