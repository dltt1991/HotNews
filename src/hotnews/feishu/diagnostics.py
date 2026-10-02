"""Read-only Feishu credential, identity, proxy, and handshake diagnostic."""

from dataclasses import replace
from typing import Mapping

from ..config import load_feishu_config
from .client import FeishuClient
from .connection import ConnectionStatus, FeishuLongConnection
from .proxy import redact_sensitive


class _IgnoreEvents:
    def handle(self, payload):
        return True


def check_feishu(environ: Mapping[str, str], timeout_seconds: float = 20) -> dict:
    config = load_feishu_config(environ)
    bot_open_id = FeishuClient(config).get_bot_open_id()
    config = replace(config, bot_open_id=bot_open_id)
    snapshot = FeishuLongConnection(config, _IgnoreEvents(), ConnectionStatus()).check(timeout_seconds)
    proxy = redact_sensitive(config.ws_proxy) if config.ws_proxy else "direct"
    return {"state": snapshot.state, "bot_identity": "resolved", "proxy": proxy}
