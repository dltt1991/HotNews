import re
from typing import Any, Dict, List, Tuple

from ..store import DeliveryStore


HELP = ("可用指令：\n订阅 AI、大模型 每天 09:00\n"
        "订阅 机器人 每隔 30 分钟\n我的订阅\n取消订阅")


def _clean_text(text: str) -> str:
    text = re.sub(r"<at[^>]*>.*?</at>", "", text, flags=re.I)
    text = re.sub(r"@[\w\u4e00-\u9fff.-]+\s*", "", text)
    return text.strip()


def parse_command(text: str) -> Tuple[str, Dict[str, Any]]:
    text = _clean_text(text)
    if text in ("帮助", "help", "/help"):
        return "help", {}
    if text in ("我的订阅", "查看订阅", "订阅列表"):
        return "list", {}
    if re.match(r"^(取消|删除|停止)订阅", text):
        return "delete", {}
    match = re.match(r"^(?:/)?订阅[：:\s]*(.+)$", text, re.S)
    if not match:
        return "unknown", {}
    body = match.group(1).strip()
    schedule: Dict[str, Any] = {"type": "daily", "at": "09:00"}
    interval = re.search(r"每隔\s*(\d+)\s*分钟", body)
    daily = re.search(r"每天\s*(\d{1,2})(?:[:：点时](\d{1,2}))?\s*分?", body)
    if interval:
        schedule = {"type": "interval", "minutes": int(interval.group(1))}
        body = body[:interval.start()] + body[interval.end():]
    elif daily:
        hour, minute = int(daily.group(1)), int(daily.group(2) or 0)
        if hour > 23 or minute > 59:
            raise ValueError("时间格式不正确")
        schedule = {"type": "daily", "at": "%02d:%02d" % (hour, minute)}
        body = body[:daily.start()] + body[daily.end():]
    keywords = [word.strip() for word in re.split(r"[,，、;；\s]+", body) if word.strip()]
    if not keywords:
        raise ValueError("请至少提供一个关键词")
    return "subscribe", {"keywords": keywords, "schedule": schedule}


def _schedule_text(schedule: Dict[str, Any]) -> str:
    if schedule.get("type") == "interval":
        return "每隔 %s 分钟" % schedule.get("minutes", 30)
    return "每天 %s" % schedule.get("at", "09:00")


def handle_command(store: DeliveryStore, platform: str, conversation_id: str,
                   text: str, channel: Dict[str, Any], sources: List[str],
                   max_items: int = 10) -> str:
    action, values = parse_command(text)
    if action in ("help", "unknown"):
        return HELP
    if action == "delete":
        return "已取消本群订阅。" if store.delete_subscription(platform, conversation_id) else "本群目前没有订阅。"
    if action == "list":
        rule = store.get_subscription(platform, conversation_id)
        if not rule:
            return "本群目前没有订阅。\n" + HELP
        return "本群订阅：%s；%s；每次最多 %s 条。" % (
            "、".join(rule["keywords"]), _schedule_text(rule["schedule"]), rule.get("max_items", 10)
        )
    rule = {
        "name": "chat:%s:%s" % (platform, conversation_id),
        "title": "热点订阅：%s" % "、".join(values["keywords"]),
        "sources": sources, "keywords": values["keywords"], "schedule": values["schedule"],
        "max_items": max_items, "channels": [channel],
    }
    store.save_subscription(platform, conversation_id, rule)
    return "订阅成功：%s；%s。" % ("、".join(values["keywords"]), _schedule_text(values["schedule"]))
