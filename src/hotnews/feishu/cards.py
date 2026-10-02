"""Feishu cards keep untrusted strings in plain-text fields and URLs in buttons."""

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence
from urllib.parse import urlparse

from ..domain import NewsResult, Subscription, ValidationError


SHANGHAI = timezone(timedelta(hours=8))


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _card(title: str, elements: list) -> dict:
    return {"config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": elements}


def _text(value: str) -> dict:
    return {"tag": "div", "text": {"tag": "plain_text", "content": value}}


def _button(title: str, url: str) -> dict:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("card links require HTTP URLs")
    return {"tag": "button", "text": {"tag": "plain_text", "content": title},
            "type": "default", "url": url}


def render_digest(subscription: Subscription, items: Sequence[NewsResult], search_window_days: int,
                  now: Optional[datetime] = None) -> dict:
    if not items:
        raise ValidationError("cannot render an empty digest")
    current = _aware(now if now is not None else datetime.now(timezone.utc))
    title = "订阅 #%d · %s · 关键词：%s · 最近 %d 天" % (
        subscription.display_number, subscription.topic, "、".join(subscription.keywords), search_window_days)
    elements = []
    for index, item in enumerate(items[:10], 1):
        published = _aware(item.published_at)
        date = published.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M")
        marker = " · 历史补充" if current - published > timedelta(hours=24) else ""
        elements.append(_text("%d. %s\n%s\n来源：%s\n发布日期：%s（UTC+8）%s" % (
            index, item.title, item.summary, item.source, date, marker)))
        actions = [_button("原文", item.url)]
        actions.extend(_button("交叉参考 %d" % n, url) for n, url in enumerate(item.references[:2], 1))
        elements.append({"tag": "action", "actions": actions})
    return _card(title, elements)


def render_command_result(message: str) -> dict:
    return _card("热点订阅", [_text(message)])
