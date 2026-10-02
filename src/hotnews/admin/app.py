"""Framework-independent localhost security and subscription JSON routes."""

from datetime import datetime, timezone
import hmac
import json
import pkgutil
import re
import secrets
import sqlite3
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qsl, urlsplit

from ..commands.schema import nonempty_string, object_fields, parse_json, parse_schedule, positive_integer
from ..config import AppConfig
from ..domain import HttpResponse, Subscription, ValidationError, VersionConflict
from ..feishu.connection import ConnectionSnapshot
from ..feishu.proxy import redact_sensitive
from ..http import ContentLengthError, bounded_content_length
from ..storage.database import Database
from ..storage.events import _utc_text
from ..storage.subscriptions import SubscriptionRepository


_STATES = {"ready", "search_terms_pending", "paused", "cancelled"}
_DEFAULT_MAX_BODY_BYTES = 64 * 1024
_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def _static_response(route: str) -> HttpResponse:
    name, content_type = _STATIC[route]
    body = pkgutil.get_data("hotnews.admin", "static/" + name)
    if body is None:
        return _json_response(500, {"error": "resource_unavailable"})
    return HttpResponse(200, {
        "Content-Type": content_type, "Content-Length": str(len(body)),
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                                   "connect-src 'self'; base-uri 'none'; object-src 'none'; "
                                   "frame-ancestors 'none'; form-action 'self'",
        "Referrer-Policy": "no-referrer",
    }, body)


def _json_response(status: int, value: dict, headers: Optional[dict] = None) -> HttpResponse:
    body = json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
    return HttpResponse(status, {"Content-Type": "application/json; charset=utf-8",
                                 "Content-Length": str(len(body)), "Cache-Control": "no-store",
                                 "X-Content-Type-Options": "nosniff", **(headers or {})}, body)


def _headers(headers: Mapping[str, str]) -> dict:
    """Retain duplicate header values so ambiguous security headers fail closed."""
    lowered = {}
    for name, value in headers.items():
        key = name.lower()
        lowered[key] = lowered[key] + "," + value if key in lowered else value
    return lowered


class AdminApplication:
    """Expose existing subscriptions; subscription creation stays in Feishu."""

    def __init__(self, config: AppConfig, repository: Optional[SubscriptionRepository] = None,
                 csrf_token: Optional[str] = None, clock: Optional[Callable[[], datetime]] = None,
                 status_provider: Optional[Callable[[], ConnectionSnapshot]] = None):
        if config.admin.host != "127.0.0.1":
            raise ValidationError("admin must bind to 127.0.0.1")
        self.config = config
        self.repository = repository if repository is not None else SubscriptionRepository(Database(config.database_path))
        self.max_body_bytes = config.admin.max_body_bytes or _DEFAULT_MAX_BODY_BYTES
        self.csrf_token = secrets.token_urlsafe(32) if csrf_token is None else nonempty_string(csrf_token, "csrf token")
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.status_provider = status_provider or (lambda: ConnectionSnapshot(
            "stopped", None, None, 0, None))

    def _connection(self) -> HttpResponse:
        snapshot = self.status_provider()
        value = {
            "state": snapshot.state,
            "connected_at": _utc_text(snapshot.connected_at) if snapshot.connected_at else None,
            "last_event_at": _utc_text(snapshot.last_event_at) if snapshot.last_event_at else None,
            "reconnect_attempts": snapshot.reconnect_attempts,
            "last_error": redact_sensitive(snapshot.last_error) if snapshot.last_error else None,
        }
        return _json_response(200, {"connection": value})

    def _authority(self, host: str) -> Optional[str]:
        match = re.fullmatch(r"(localhost|127\.0\.0\.1)(?::([0-9]{1,5}))?", host, re.IGNORECASE)
        if match is None:
            return None
        port_text = match.group(2)
        if port_text is None:
            if self.config.admin.port != 80:
                return None
        elif port_text != str(self.config.admin.port):
            return None
        name = match.group(1).lower()
        return name if self.config.admin.port == 80 else "%s:%d" % (name, self.config.admin.port)

    def _item(self, sub: Subscription, names: Mapping[str, Optional[str]]) -> dict:
        schedule = {"kind": sub.schedule.kind}
        if sub.schedule.kind == "daily":
            schedule["daily_at"] = sub.schedule.daily_at
        elif sub.schedule.kind == "interval":
            schedule["interval_minutes"] = sub.schedule.interval_minutes
        value = {"id": sub.id, "chat_id": sub.chat_id, "chat_name": names.get(sub.chat_id) or sub.chat_id,
                 "display_number": sub.display_number, "creator_id": sub.creator_id, "topic": sub.topic,
                 "keywords": list(sub.keywords), "search_terms": list(sub.search_terms),
                 "search_terms_status": "ready" if sub.search_terms else "pending", "schedule": schedule,
                 "timezone": self.config.timezone, "state": sub.state, "version": sub.version,
                 "consecutive_failures": sub.consecutive_failures, "alerted": sub.alerted}
        for name in ("next_run_at", "created_at", "updated_at", "cancelled_at", "last_success_at"):
            instant = getattr(sub, name)
            value[name] = _utc_text(instant) if instant is not None else None
        return value

    def _single(self, sub: Subscription, **extra) -> HttpResponse:
        return _json_response(200, {"subscription": self._item(sub, self.repository.chat_names()), **extra})

    def _list(self, query: str) -> HttpResponse:
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=4) if query else []
        except (ValueError, UnicodeError):
            raise ValidationError("invalid filters") from None
        filters = {}
        for name, value in pairs:
            if name not in ("chat_id", "status", "keyword", "include_cancelled") or name in filters:
                raise ValidationError("invalid filters")
            filters[name] = value
        for name in ("chat_id", "keyword"):
            if name in filters:
                nonempty_string(filters[name], name)
        if "status" in filters and filters["status"] not in _STATES:
            raise ValidationError("invalid status")
        include = filters.get("include_cancelled", "false")
        if include not in ("true", "false"):
            raise ValidationError("invalid history filter")
        rows = self.repository.list(filters.get("chat_id"), include_cancelled=include == "true")
        if "status" in filters:
            rows = [sub for sub in rows if sub.state == filters["status"]]
        if "keyword" in filters:
            needle = filters["keyword"].casefold()
            rows = [sub for sub in rows if needle in sub.topic.casefold()
                    or any(needle in word.casefold() for word in sub.keywords)]
        names = self.repository.chat_names()
        return _json_response(200, {"subscriptions": [self._item(sub, names) for sub in rows]})

    def _mutate(self, method: str, subscription_id: str, action: Optional[str], body: bytes) -> HttpResponse:
        value = parse_json(body)
        object_fields(value, ("version",), ("topic", "keywords", "schedule") if method == "PATCH" else ())
        version = positive_integer(value["version"], "version")
        instant = self.clock()
        if method == "PATCH":
            if len(value) == 1:
                raise ValidationError("edit requires at least one field")
            updates = {}
            if "topic" in value:
                updates["topic"] = nonempty_string(value["topic"], "topic")
            if "keywords" in value:
                if not isinstance(value["keywords"], list):
                    raise ValidationError("keywords must be an array")
                updates["keywords"] = value["keywords"]
            if "schedule" in value:
                updates["schedule"] = parse_schedule(value["schedule"])
            return self._single(self.repository.update(subscription_id, version, now=instant, **updates))
        if method == "DELETE":
            return self._single(self.repository.cancel(subscription_id, version, now=instant))
        if action == "pause":
            return self._single(self.repository.pause(subscription_id, version, now=instant))
        if action == "resume":
            return self._single(self.repository.resume(subscription_id, version, now=instant))
        run_id = self.repository.request_manual_run(subscription_id, version, now=instant)
        return self._single(self.repository.get(subscription_id), run_id=run_id)

    def handle(self, method: str, path: str, headers: Mapping[str, str], body: bytes) -> HttpResponse:
        """Check request boundaries before calling transactional repository actions."""
        lowered = _headers(headers)
        authority = self._authority(lowered.get("host", ""))
        if authority is None:
            return _json_response(403, {"error": "forbidden"})
        try:
            parsed = urlsplit(path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise ValidationError("invalid route")
            route = parsed.path
            subscription_id, action = None, None
            if route in _STATIC or route in ("/api/session", "/api/subscriptions", "/api/connection"):
                allowed = "GET"
            else:
                match = re.fullmatch(r"/api/subscriptions/([^/]+)(?:/(pause|resume|run-now))?", route)
                if match is None:
                    return _json_response(404, {"error": "not_found"})
                subscription_id, action = match.groups()
                allowed = "POST" if action is not None else "GET, PATCH, DELETE"
            if method not in allowed.split(", "):
                return _json_response(405, {"error": "method_not_allowed"}, {"Allow": allowed})
            if len(body) > self.max_body_bytes:
                return _json_response(413, {"error": "body_too_large"})
            declared = lowered.get("content-length")
            if declared is not None:
                try:
                    length = bounded_content_length(declared, self.max_body_bytes)
                except ContentLengthError as error:
                    return _json_response(error.status, {"error": "body_too_large" if error.status == 413 else "invalid_request"})
                if length != len(body):
                    raise ValidationError("invalid request framing")
            if "transfer-encoding" in lowered:
                raise ValidationError("unsupported request framing")
            if route in _STATIC:
                if parsed.query:
                    raise ValidationError("static routes do not accept filters")
                return _static_response(route)
            if method != "GET":
                if (lowered.get("origin") != "http://" + authority
                        or not hmac.compare_digest(lowered.get("x-hotnews-csrf", "").encode("utf-8"),
                                                   self.csrf_token.encode("utf-8"))):
                    return _json_response(403, {"error": "forbidden"})
                if lowered.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    return _json_response(415, {"error": "requires_json"})
            if route == "/api/session":
                if parsed.query:
                    raise ValidationError("session does not accept filters")
                return _json_response(200, {"csrf_token": self.csrf_token})
            if route == "/api/connection":
                if parsed.query:
                    raise ValidationError("connection route does not accept filters")
                return self._connection()
            if route == "/api/subscriptions":
                return self._list(parsed.query)
            if parsed.query:
                raise ValidationError("subscription route does not accept filters")
            if method == "GET":
                sub = self.repository.get(subscription_id)
                return _json_response(404, {"error": "not_found"}) if sub is None else self._single(sub)
            return self._mutate(method, subscription_id, action, body)
        except VersionConflict:
            return _json_response(409, {"error": "version_conflict"})
        except KeyError:
            return _json_response(404, {"error": "not_found"})
        except (ValidationError, ValueError, UnicodeError, RecursionError):
            return _json_response(400, {"error": "invalid_request"})
        except (sqlite3.Error, OSError):
            return _json_response(500, {"error": "storage_unavailable"})
