"""Non-secret application configuration and runtime-only Feishu credentials."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .domain import ValidationError


@dataclass(frozen=True)
class FeishuConfig:
    app_id: str
    app_secret: str = field(repr=False)
    bot_open_id: Optional[str] = None
    ws_proxy: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    max_body_bytes: Optional[int] = None


@dataclass(frozen=True)
class WorkerConfig:
    max_queued_events: int = 20
    max_due_subscriptions: int = 3
    lease_seconds: int = 900
    soft_budget_seconds: int = 240
    outbox_max_attempts: int = 5
    outbox_backoff_seconds: int = 10
    outbox_backoff_max_seconds: int = 300


@dataclass(frozen=True)
class AppConfig:
    database_path: str = "data/hotnews.db"
    admin: ServerConfig = ServerConfig("127.0.0.1", 8081)
    worker: WorkerConfig = WorkerConfig()
    timezone: str = "Asia/Shanghai"
    max_results: int = 10


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValidationError("%s must be an object" % name)
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValidationError("%s must be a positive integer" % name)
    return value


def _server(value: object, default: ServerConfig, name: str) -> ServerConfig:
    data = _mapping(value, name) if value is not None else {}
    host = data.get("host", default.host)
    port = data.get("port", default.port)
    if not isinstance(host, str) or not host.strip():
        raise ValidationError("%s.host must be a non-empty string" % name)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValidationError("%s.port must be between 1 and 65535" % name)
    max_body_bytes = data.get("max_body_bytes", default.max_body_bytes)
    if max_body_bytes is not None:
        max_body_bytes = _positive_int(max_body_bytes, "%s.max_body_bytes" % name)
    return ServerConfig(host, port, max_body_bytes)


def _worker(value: object) -> WorkerConfig:
    defaults = WorkerConfig()
    data = _mapping(value, "worker") if value is not None else {}
    names = (
        "max_queued_events", "max_due_subscriptions", "lease_seconds",
        "soft_budget_seconds", "outbox_max_attempts", "outbox_backoff_seconds",
        "outbox_backoff_max_seconds",
    )
    values = {name: _positive_int(data.get(name, getattr(defaults, name)), "worker." + name)
              for name in names}
    return WorkerConfig(**values)


def load_config(path: str) -> AppConfig:
    """Load non-secret settings from JSON; credential environment variables are not read."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValidationError("could not load config: %s" % exc)
    data = _mapping(data, "config")
    if "callback" in data:
        raise ValidationError("callback configuration is obsolete; use Feishu long connection")
    database_path = data.get("database_path", "data/hotnews.db")
    timezone = data.get("timezone", "Asia/Shanghai")
    max_results = _positive_int(data.get("max_results", 10), "max_results")
    if max_results > 10:
        raise ValidationError("max_results cannot exceed 10")
    if not isinstance(database_path, str) or not database_path.strip():
        raise ValidationError("database_path must be a non-empty string")
    if timezone != "Asia/Shanghai":
        raise ValidationError("timezone must be Asia/Shanghai")
    admin_default = ServerConfig("127.0.0.1", 8081)
    return AppConfig(
        database_path=database_path,
        admin=_server(data.get("admin"), admin_default, "admin"),
        worker=_worker(data.get("worker")),
        timezone=timezone,
        max_results=max_results,
    )


def load_feishu_config(environ: Mapping[str, str]) -> FeishuConfig:
    """Load Feishu credentials only from the supplied runtime environment."""
    from .feishu.proxy import resolve_ws_proxy

    required = ("FEISHU_APP_ID", "FEISHU_APP_SECRET")
    missing = [name for name in required if not environ.get(name, "").strip()]
    if missing:
        raise ValidationError("missing required Feishu environment variables: %s" % ", ".join(missing))
    return FeishuConfig(
        app_id=environ["FEISHU_APP_ID"],
        app_secret=environ["FEISHU_APP_SECRET"],
        ws_proxy=resolve_ws_proxy(environ),
    )
