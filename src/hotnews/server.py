import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

from .channels import send_feishu_text
from .commands import handle_command
from .crypto import aes_decrypt, feishu_decrypt, wecom_decrypt, wecom_encrypt, wecom_key, wecom_signature
from .http import post_json
from .store import DeliveryStore


LOGGER = logging.getLogger(__name__)


def _text_from_feishu(event: Dict[str, Any]) -> str:
    message = event.get("message", {})
    if message.get("message_type") != "text":
        return ""
    content = message.get("content", "{}")
    return json.loads(content).get("text", "") if isinstance(content, str) else content.get("text", "")


class CallbackApp:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.inbound = config.get("inbound", {})
        self.store = DeliveryStore(config.get("database", "data/hotnews.db"))
        self.sources = [source["name"] for source in config["sources"]]

    def command(self, platform: str, conversation_id: str, text: str, channel: Dict[str, Any]) -> str:
        return handle_command(self.store, platform, conversation_id, text, channel, self.sources,
                              int(self.inbound.get("default_max_items", 10)))

    def feishu(self, body: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.inbound.get("feishu", {})
        if "encrypt" in body:
            body = feishu_decrypt(body["encrypt"], settings["encrypt_key"])
        if body.get("type") == "url_verification":
            if settings.get("verification_token") and body.get("token") != settings["verification_token"]:
                raise PermissionError("invalid Feishu verification token")
            return {"challenge": body["challenge"]}
        header = body.get("header", {})
        if settings.get("verification_token") and header.get("token") != settings["verification_token"]:
            raise PermissionError("invalid Feishu verification token")
        if header.get("event_type") != "im.message.receive_v1":
            return {}
        event_id = header.get("event_id", "")
        if event_id and not self.store.claim_event("feishu:" + event_id):
            return {}
        event, text = body.get("event", {}), _text_from_feishu(body.get("event", {}))
        chat_id = event.get("message", {}).get("chat_id")
        if not chat_id or not text:
            return {}
        channel = {"name": "feishu:" + chat_id, "type": "feishu_app", "chat_id": chat_id,
                   "app_id": settings["app_id"], "app_secret": settings["app_secret"]}
        send_feishu_text(channel, self.command("feishu", chat_id, text, channel))
        return {}

    def wecom(self, body: Dict[str, Any], query: Dict[str, Any]) -> Dict[str, Any]:
        settings = self.inbound.get("wecom", {})
        encrypted = body.get("encrypt", "")
        signature = (query.get("msg_signature") or query.get("msgsignature") or [""])[0]
        timestamp, nonce = query.get("timestamp", [""])[0], query.get("nonce", [""])[0]
        if signature != wecom_signature(settings["token"], timestamp, nonce, encrypted):
            raise PermissionError("invalid WeCom signature")
        message = wecom_decrypt(encrypted, settings["encoding_aes_key"])
        event_id = message.get("msgid", "")
        if event_id and not self.store.claim_event("wecom:" + event_id):
            return {}
        text = message.get("text", {}).get("content", "")
        if message.get("msgtype") == "voice":
            text = message.get("voice", {}).get("content", "")
        chat_id = message.get("chatid") or "single:" + message.get("from", {}).get("userid", "")
        webhook = settings.get("group_webhooks", {}).get(chat_id)
        channel = {"name": "wecom:" + chat_id, "type": "wecom", "webhook": webhook or ""}
        if not webhook:
            reply = "未配置本群定时推送 Webhook（chatid: %s）。请管理员设置 inbound.wecom.group_webhooks。" % chat_id
        else:
            reply = self.command("wecom", chat_id, text, channel)
        if message.get("response_url"):
            post_json(message["response_url"], {"msgtype": "markdown", "markdown": {"content": reply}})
            return {}
        return wecom_encrypt({"msgtype": "stream", "stream": {"id": event_id, "finish": True,
                              "content": reply}}, settings["token"], settings["encoding_aes_key"],
                             message.get("aibotid", ""))


def make_handler(app: CallbackApp):
    class Handler(BaseHTTPRequestHandler):
        def _write(self, status: int, payload: Any) -> None:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                return self._write(200, {"status": "ok"})
            if parsed.path == "/callbacks/wecom":
                query = parse_qs(parsed.query)
                settings = app.inbound.get("wecom", {})
                encrypted = query.get("echostr", [""])[0]
                signature = query.get("msg_signature", [""])[0]
                timestamp, nonce = query.get("timestamp", [""])[0], query.get("nonce", [""])[0]
                if signature != wecom_signature(settings["token"], timestamp, nonce, encrypted):
                    return self._write(403, {"error": "invalid signature"})
                return self._write(200, aes_decrypt(encrypted, wecom_key(settings["encoding_aes_key"]), True))
            self._write(404, {"error": "not found"})

        def do_POST(self):
            try:
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size).decode("utf-8"))
                parsed = urlparse(self.path)
                if parsed.path == "/callbacks/feishu":
                    return self._write(200, app.feishu(body))
                if parsed.path == "/callbacks/wecom":
                    return self._write(200, app.wecom(body, parse_qs(parsed.query)))
                self._write(404, {"error": "not found"})
            except PermissionError as error:
                self._write(403, {"error": str(error)})
            except Exception as error:
                LOGGER.exception("callback failed")
                self._write(500, {"error": str(error)})

        def log_message(self, fmt, *args):
            LOGGER.info("callback: " + fmt, *args)
    return Handler


def serve_callbacks(config: Dict[str, Any]) -> None:
    settings = config.get("inbound", {})
    host, port = settings.get("host", "0.0.0.0"), int(settings.get("port", 8080))
    LOGGER.info("callback server listening on %s:%d", host, port)
    ThreadingHTTPServer((host, port), make_handler(CallbackApp(config))).serve_forever()
