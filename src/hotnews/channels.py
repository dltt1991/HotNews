import base64
import hashlib
import hmac
import time
from typing import Any, Dict, List

from .http import post_json
from .models import NewsItem


def _feishu_token(channel: Dict[str, Any]) -> str:
    response = post_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {
        "app_id": channel["app_id"], "app_secret": channel["app_secret"]
    })
    if response.get("code") != 0 or not response.get("tenant_access_token"):
        raise RuntimeError("Feishu authentication failed: %s" % response)
    return response["tenant_access_token"]


def send_feishu_text(channel: Dict[str, Any], text: str) -> Dict[str, Any]:
    import json
    token = _feishu_token(channel)
    response = post_json(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": channel["chat_id"], "msg_type": "text",
         "content": json.dumps({"text": text}, ensure_ascii=False)},
        headers={"Authorization": "Bearer " + token},
    )
    if response.get("code") != 0:
        raise RuntimeError("Feishu send failed: %s" % response)
    return response


def render_markdown(title: str, items: List[NewsItem]) -> str:
    lines = ["## %s" % title, ""]
    for index, item in enumerate(items, 1):
        meta = " · ".join(value for value in [item.source, item.published_text] if value)
        lines.append("%d. [%s](%s)%s" % (index, item.title, item.url, "  \n   %s" % meta if meta else ""))
    return "\n".join(lines)


def _feishu_signature(timestamp: str, secret: str) -> str:
    key = (timestamp + "\n" + secret).encode("utf-8")
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def send(channel: Dict[str, Any], title: str, items: List[NewsItem]) -> Dict[str, Any]:
    kind = channel["type"]
    content = render_markdown(title, items)
    if kind == "feishu":
        payload: Dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text", "content": title}},
                "elements": [{"tag": "markdown", "content": content}],
            },
        }
        if channel.get("secret"):
            timestamp = str(int(time.time()))
            payload.update(timestamp=timestamp, sign=_feishu_signature(timestamp, channel["secret"]))
        response = post_json(channel["webhook"], payload)
        if response.get("code", response.get("StatusCode", 0)) != 0:
            raise RuntimeError("Feishu webhook failed: %s" % response)
        return response
    if kind == "wecom":
        response = post_json(channel["webhook"], {
            "msgtype": "markdown",
            "markdown": {"content": content},
        })
        if response.get("errcode", 0) != 0:
            raise RuntimeError("WeCom webhook failed: %s" % response)
        return response
    if kind == "feishu_app":
        import json
        token = _feishu_token(channel)
        response = post_json(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            {"receive_id": channel["chat_id"], "msg_type": "interactive",
             "content": json.dumps({
                 "header": {"title": {"tag": "plain_text", "content": title}},
                 "elements": [{"tag": "markdown", "content": content}],
             }, ensure_ascii=False)},
            headers={"Authorization": "Bearer " + token},
        )
        if response.get("code") != 0:
            raise RuntimeError("Feishu send failed: %s" % response)
        return response
    raise ValueError("unsupported channel type: %s" % kind)
