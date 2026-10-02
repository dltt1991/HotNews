"""Apply commands together with their response and event completion."""

from dataclasses import asdict, fields, is_dataclass
from datetime import datetime, timezone
import json

from .schema import nonempty_string, object_fields, parse_intent, parse_json, positive_integer
from ..domain import CommandResult, Intent, LeaseConflict, Schedule, Subscription, ValidationError
from ..feishu.cards import render_command_result
from ..storage.database import Database
from ..storage.events import EventRepository, _datetime, _utc_text
from ..storage.outbox import OutboxRepository
from ..storage.subscriptions import SHANGHAI, SubscriptionRepository


HELP = ("请在群内 @机器人，使用以下指令：\n"
        "订阅 AI Agent 和大模型，每天 09:00\n"
        "订阅 新能源汽车，每隔 2 小时\n"
        "查看订阅\n取消订阅 2\n立即推送 1\n帮助\n"
        "未指定时间时默认每天 09:00（Asia/Shanghai）。")


def json_default(value):
    """Shared JSON encoding for immutable domain records and UTC timestamps."""
    if isinstance(value, datetime):
        return _utc_text(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError("unsupported JSON value")


def _restore_subscription(value):
    object_fields(value, tuple(field.name for field in fields(Subscription)))
    restored = dict(value)
    for name in ("id", "chat_id", "creator_id", "topic"):
        nonempty_string(restored[name], name)
    for name in ("display_number", "version"):
        positive_integer(restored[name], name)
    count = restored["consecutive_failures"]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValidationError("invalid stored failure count")
    if type(restored["alerted"]) is not bool or restored["state"] not in (
            "ready", "search_terms_pending", "paused", "cancelled"):
        raise ValidationError("invalid stored subscription state")
    for name in ("keywords", "search_terms"):
        if not isinstance(restored[name], list):
            raise ValidationError("invalid stored keyword list")
        restored[name] = tuple(nonempty_string(item, name) for item in restored[name])
    schedule = restored["schedule"]
    object_fields(schedule, ("kind", "daily_at", "interval_minutes"))
    restored["schedule"] = Schedule(**schedule)
    for name in ("created_at", "updated_at", "next_run_at", "cancelled_at", "last_success_at"):
        raw = restored[name]
        if raw is None and name not in ("created_at", "updated_at"):
            continue
        nonempty_string(raw, name)
        restored[name] = _datetime(raw)
        _utc_text(restored[name])
    return Subscription(**restored)


def _restore_result(raw):
    try:
        value = parse_json(raw)
        object_fields(value, ("message", "subscription", "subscriptions"))
        nonempty_string(value["message"], "message")
        if not isinstance(value["subscriptions"], list):
            raise ValidationError("invalid stored subscription list")
        return CommandResult(
            value["message"],
            _restore_subscription(value["subscription"]) if value["subscription"] is not None else None,
            tuple(_restore_subscription(item) for item in value["subscriptions"]),
        )
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValidationError("stored command result is invalid") from None


def _validated_intent(intent):
    if not isinstance(intent, Intent):
        raise ValidationError("intent must be a validated Intent")
    value = {"action": intent.action}
    for name in ("topic", "schedule", "subscription_number"):
        field = getattr(intent, name)
        if field is not None:
            if name == "schedule":
                if not isinstance(field, Schedule):
                    raise ValidationError("invalid intent schedule")
                field = {"kind": field.kind}
                if intent.schedule.daily_at is not None:
                    field["daily_at"] = intent.schedule.daily_at
                if intent.schedule.interval_minutes is not None:
                    field["interval_minutes"] = intent.schedule.interval_minutes
            value[name] = field
    for name in ("keywords", "search_terms"):
        if getattr(intent, name):
            value[name] = list(getattr(intent, name))
    return parse_intent(value)


def _description(subscription):
    schedule = subscription.schedule
    if schedule.kind == "daily":
        timing = "每天 %s" % schedule.daily_at
    elif schedule.kind == "interval":
        timing = "每隔 %d 分钟" % schedule.interval_minutes
    else:
        timing = "仅手动推送"
    due = subscription.next_run_at.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M") if subscription.next_run_at else "无"
    state = {"ready": "启用", "paused": "暂停", "search_terms_pending": "等待更新搜索词", "cancelled": "已取消"}
    return "#%d %s；关键词：%s；%s；下次：%s（Asia/Shanghai）；%s" % (
        subscription.display_number, subscription.topic, "、".join(subscription.keywords), timing, due, state[subscription.state])


class CommandService:
    def __init__(self, database: Database, clock=None):
        self.database = database
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events = EventRepository(database)
        self.subscriptions = SubscriptionRepository(database)
        self.outbox = OutboxRepository(database)

    def apply(self, event_id: str, owner: str, intent: Intent) -> CommandResult:
        nonempty_string(event_id, "event_id")
        nonempty_string(owner, "owner")
        intent = _validated_intent(intent)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = self.events.get(event_id, connection=connection)
            if event is None:
                raise LeaseConflict("event is not leased by this owner")
            if event.status == "completed":
                return _restore_result(self.events.get_result(event_id, connection=connection))
            now = self.clock()
            _utc_text(now)
            if (event.status != "leased" or event.lease_owner != owner
                    or event.lease_until is None or event.lease_until <= now):
                raise LeaseConflict("event is not leased by this owner")
            result = self._execute(event, intent, now, connection)
            self.outbox.enqueue(event.chat_id, "card", render_command_result(result.message),
                                "event:%s:result" % event.event_id, connection=connection)
            self.events.complete(event_id, owner, json.dumps(result, default=json_default, ensure_ascii=True,
                                                           allow_nan=False), connection=connection, now=now)
            return result

    def _execute(self, event, intent, now, connection):
        action = intent.action
        if action == "create_subscription":
            subscription = self.subscriptions.create(event.chat_id, event.sender_id, intent.topic,
                                                      intent.keywords, intent.search_terms, intent.schedule, now,
                                                      connection=connection)
            return CommandResult("订阅成功：" + _description(subscription), subscription)
        if action == "list_subscriptions":
            values = tuple(self.subscriptions.list(event.chat_id, connection=connection))
            message = "本群订阅：\n" + "\n".join(_description(item) for item in values) if values else "本群目前没有订阅。\n" + HELP
            return CommandResult(message, subscriptions=values)
        if action in ("cancel_subscription", "run_subscription_now"):
            subscription = self.subscriptions.get_by_number(event.chat_id, intent.subscription_number,
                                                            connection=connection)
            if subscription is None or subscription.state == "cancelled":
                raise ValidationError("subscription number is not active in this group")
            if action == "cancel_subscription":
                subscription = self.subscriptions.cancel(subscription.id, subscription.version, now,
                                                          connection=connection)
                return CommandResult("已取消订阅 #%d。" % subscription.display_number, subscription)
            self.subscriptions.request_manual_run(subscription.id, subscription.version, now, connection=connection)
            subscription = self.subscriptions.get(subscription.id, connection=connection)
            return CommandResult("已安排立即推送订阅 #%d，将在 5 分钟内处理。" % subscription.display_number, subscription)
        prefix = "指令含义不明确，请提供主题、时间或订阅编号。\n" if action == "clarification_required" else ""
        return CommandResult(prefix + HELP)
