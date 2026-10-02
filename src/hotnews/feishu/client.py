"""Injectable Feishu application client with cached tenant tokens."""

import json
import math
import time
from threading import Lock
from typing import Callable, Optional

from ..config import FeishuConfig
from ..domain import HttpResponse, ValidationError
from ..http import HttpTransport, UrllibTransport


BASE_URL = "https://open.feishu.cn/open-apis"
_AUTH_CODES = {99991663, 99991665}


class FeishuAPIError(RuntimeError):
    """Sanitized failure metadata for the outbox's retry policy; no response text."""

    def __init__(self, message: str, status: Optional[int] = None,
                 code: Optional[int] = None, retry_after: Optional[float] = None,
                 retryable: bool = False):
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.retryable = retryable
        super().__init__(message)


def _failure(response: HttpResponse, payload: dict) -> FeishuAPIError:
    code = payload.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        code = None
    retry_after = None
    for key, value in response.headers.items():
        if key.lower() == "retry-after":
            try:
                delay = float(value)
                if math.isfinite(delay) and delay >= 0:
                    retry_after = delay
            except (TypeError, ValueError):
                pass
    retryable = (response.status in (401, 429) or response.status >= 500
                 or code == 99991400 or code in _AUTH_CODES)
    return FeishuAPIError("Feishu API request failed (HTTP %d, code %s)" % (response.status, code),
                          response.status, code, retry_after, retryable)


def _successful(response: HttpResponse, payload: dict) -> bool:
    code = payload.get("code")
    return 200 <= response.status < 300 and type(code) is int and code == 0


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a nonempty string" % name)


class FeishuClient:
    def __init__(self, config: FeishuConfig, transport: Optional[HttpTransport] = None,
                 clock: Callable[[], float] = time.monotonic, timeout: float = 15):
        self.config = config
        self.transport = transport if transport is not None else UrllibTransport()
        self.clock = clock
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._token_lock = Lock()
        self._bot_open_id = config.bot_open_id

    def _request(self, method: str, path: str, body: Optional[bytes] = None,
                 access_token: Optional[str] = None):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if access_token is not None:
            headers["Authorization"] = "Bearer " + access_token
        try:
            response = self.transport.request(method, BASE_URL + path, headers=headers,
                                              body=body, timeout=self.timeout)
        except OSError:
            raise FeishuAPIError("Feishu network request failed", retryable=True) from None
        try:
            payload = json.loads(response.body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("response must be an object")
        except (ValueError, UnicodeError):
            raise FeishuAPIError("invalid Feishu API response", status=response.status,
                                 retryable=response.status == 429 or response.status >= 500) from None
        return response, payload

    def _tenant_token(self, refresh: bool = False) -> str:
        with self._token_lock:
            started = self.clock()
            if not refresh and self._token is not None and started < self._token_expires:
                return self._token
            self._token = None
            self._token_expires = 0.0
            body = json.dumps({"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                              ensure_ascii=False).encode("utf-8")
            response, payload = self._request("POST", "/auth/v3/tenant_access_token/internal", body)
            if not _successful(response, payload):
                raise _failure(response, payload) from None
            token = payload.get("tenant_access_token")
            expire = payload.get("expire")
            if (not isinstance(token, str) or not token.strip()
                    or type(expire) is not int or expire <= 0):
                raise FeishuAPIError("invalid Feishu authentication response", status=response.status) from None
            self._token = token
            self._token_expires = started + expire - min(60, expire / 10)
            return token

    def _authenticated(self, method: str, path: str, body: Optional[bytes] = None) -> dict:
        access_token = self._tenant_token()
        for attempt in range(2):
            response, payload = self._request(method, path, body, access_token)
            if _successful(response, payload):
                return payload
            code = payload.get("code")
            auth_failure = response.status == 401 or (type(code) is int and code in _AUTH_CODES)
            if attempt == 0 and auth_failure:
                access_token = self._tenant_token(refresh=True)
                continue
            raise _failure(response, payload) from None
        raise AssertionError("unreachable")

    def get_bot_open_id(self) -> str:
        """Resolve the application's bot identity once before callback startup."""
        if isinstance(self._bot_open_id, str) and self._bot_open_id.strip():
            return self._bot_open_id
        payload = self._authenticated("GET", "/bot/v3/info")
        bot = payload.get("bot")
        identity = bot.get("open_id") if isinstance(bot, dict) else None
        if not isinstance(identity, str) or not identity.strip():
            raise FeishuAPIError("Feishu bot identity is missing") from None
        self._bot_open_id = identity
        return identity

    def _send(self, chat_id: str, kind: str, content: dict, idempotency_key: str) -> dict:
        _nonempty(chat_id, "chat_id")
        _nonempty(idempotency_key, "idempotency_key")
        try:
            payload = {"receive_id": chat_id, "msg_type": kind,
                       "content": json.dumps(content, ensure_ascii=False, allow_nan=False),
                       "uuid": idempotency_key}
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            raise ValidationError("message content must be JSON serializable") from None
        return self._authenticated("POST", "/im/v1/messages?receive_id_type=chat_id", body)

    def send_text(self, chat_id: str, text: str, idempotency_key: str) -> dict:
        if not isinstance(text, str):
            raise ValidationError("message text must be a string")
        return self._send(chat_id, "text", {"text": text}, idempotency_key)

    def send_card(self, chat_id: str, card: dict, idempotency_key: str) -> dict:
        if not isinstance(card, dict):
            raise ValidationError("message card must be an object")
        return self._send(chat_id, "interactive", card, idempotency_key)
