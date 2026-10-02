"""Pinned lark-oapi 1.7.3 adapter; all SDK private access lives here."""

import asyncio
import http
from importlib import metadata
import json
import socket
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse, urlsplit

from ..config import FeishuConfig
from .connection import FatalConnectionError, SDKCompatibilityError


_SDK_VERSION = "1.7.3"


def verify_sdk_version(version: str) -> None:
    if version != _SDK_VERSION:
        raise SDKCompatibilityError(
            "unsupported lark-oapi version; expected %s" % _SDK_VERSION)


def sdk_event_payload(event: object, marshal: Callable[[object], str]) -> dict:
    try:
        payload = json.loads(marshal(event))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise SDKCompatibilityError("could not decode Feishu SDK event") from None
    if not isinstance(payload, dict):
        raise SDKCompatibilityError("Feishu SDK event must be an object")
    return payload


def proxy_socket_settings(proxy_url: str) -> dict:
    parsed = urlsplit(proxy_url)
    scheme = "socks5" if parsed.scheme in ("socks5", "socks5h") else parsed.scheme
    return {
        "scheme": scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "remote_dns": parsed.scheme in ("http", "https", "socks5h"),
        "username": unquote(parsed.username) if parsed.username is not None else None,
        "password": unquote(parsed.password) if parsed.password is not None else None,
    }


def _open_proxy_socket(proxy_url: str, host: str, port: int):
    try:
        import socks
    except ImportError:
        raise SDKCompatibilityError("PySocks dependency is not installed") from None
    settings = proxy_socket_settings(proxy_url)
    proxy_type = socks.SOCKS5 if settings["scheme"] == "socks5" else socks.HTTP
    result = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
    result.set_proxy(proxy_type, settings["host"], settings["port"],
                     rdns=settings["remote_dns"], username=settings["username"],
                     password=settings["password"])
    result.settimeout(15)
    try:
        result.connect((host, port))
        result.settimeout(None)
        return result
    except Exception:
        result.close()
        raise


def _client_class(ws_module):
    class ProxyAwareClient(ws_module.Client):
        def __init__(self, *args, proxy_url=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._hotnews_proxy_url = proxy_url
            self._hotnews_receive_task = None

        def _get_conn_url(self):
            if not self._app_id or not self._app_secret:
                raise ws_module.ClientException(ws_module.NO_CREDENTIAL, "credentials are required")
            headers = dict(self._headers)
            headers.update({"locale": "zh", ws_module.USER_AGENT: self._user_agent})
            body = {"AppID": self._app_id, "AppSecret": self._app_secret}
            proxies = None
            if self._hotnews_proxy_url:
                proxies = {"http": self._hotnews_proxy_url, "https": self._hotnews_proxy_url}
            response = ws_module.requests.post(
                self._domain + ws_module.GEN_ENDPOINT_URI, headers=headers, json=body,
                proxies=proxies, timeout=15)
            if response.status_code != http.HTTPStatus.OK:
                raise ws_module.ServerException(response.status_code, "endpoint discovery failed")
            resp = ws_module.JSON.unmarshal(str(response.content, ws_module.UTF_8),
                                            ws_module.EndpointResp)
            if resp.code != ws_module.OK:
                error_type = ws_module.ServerException if resp.code in (
                    ws_module.SYSTEM_BUSY, ws_module.INTERNAL_ERROR) else ws_module.ClientException
                raise error_type(resp.code, "endpoint discovery failed")
            if resp.data.ClientConfig is not None:
                self._configure(resp.data.ClientConfig)
            return resp.data.URL

        async def _connect(self):
            async with self._lock:
                if self._conn is not None:
                    return
                loop = asyncio.get_running_loop()
                conn_url = await loop.run_in_executor(None, self._get_conn_url)
                parsed = urlparse(conn_url)
                query = parse_qs(parsed.query)
                conn_id = query[ws_module.DEVICE_ID][0]
                service_id = query[ws_module.SERVICE_ID][0]
                kwargs = {}
                if self._hotnews_proxy_url:
                    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
                    sock = await loop.run_in_executor(
                        None, _open_proxy_socket, self._hotnews_proxy_url, parsed.hostname, port)
                    kwargs.update(sock=sock, server_hostname=parsed.hostname)
                self._conn = await ws_module.websockets.connect(conn_url, **kwargs)
                self._conn_url = conn_url
                self._conn_id = conn_id
                self._service_id = service_id
                self._hotnews_receive_task = loop.create_task(self._receive_message_loop())

        async def _receive_message_loop(self):
            try:
                while True:
                    if self._conn is None:
                        raise ws_module.ConnectionClosedException("connection is closed")
                    message = await self._conn.recv()
                    asyncio.get_running_loop().create_task(self._handle_message(message))
            except asyncio.CancelledError:
                raise
            except Exception:
                await self._disconnect()
                if self._auto_reconnect:
                    await self._reconnect()
                else:
                    raise

        async def _disconnect(self):
            async with self._lock:
                if self._conn is not None:
                    await self._conn.close()
                self._conn = None
                self._conn_url = ""
                self._conn_id = ""
                self._service_id = ""

    return ProxyAwareClient


class _SDKConnector:
    def __init__(self, config: FeishuConfig, event_callback):
        try:
            import lark_oapi as lark
            from lark_oapi.ws import client as ws_module
        except ImportError:
            raise SDKCompatibilityError("lark-oapi dependency is not installed") from None
        try:
            version = metadata.version("lark-oapi")
        except metadata.PackageNotFoundError:
            raise SDKCompatibilityError("lark-oapi package metadata is unavailable") from None
        verify_sdk_version(version)
        self._ws_module = ws_module
        self._client_error = ws_module.ClientException
        self._event_callback = event_callback

        def receive(event):
            event_callback(sdk_event_payload(event, lark.JSON.marshal))

        dispatcher = (lark.EventDispatcherHandler.builder("", "")
                      .register_p2_im_message_receive_v1(receive).build())
        client_type = _client_class(ws_module)
        self._client = client_type(config.app_id, config.app_secret,
                                   event_handler=dispatcher, proxy_url=config.ws_proxy)
        self._closed = False

    async def _run(self, stop_event, on_connected, on_reconnecting):
        self._client.on_reconnecting = lambda: on_reconnecting("connection lost")
        self._client.on_reconnected = on_connected
        await self._client._connect()
        on_connected()
        ping = asyncio.create_task(self._client._ping_loop())
        try:
            while not stop_event.is_set():
                receive = self._client._hotnews_receive_task
                if receive is not None and receive.done():
                    await receive
                    raise OSError("Feishu receive loop stopped")
                await asyncio.sleep(0.2)
        finally:
            ping.cancel()
            receive = self._client._hotnews_receive_task
            if receive is not None:
                receive.cancel()
            await self._client._disconnect()

    def run(self, stop_event, on_connected, on_reconnecting):
        try:
            asyncio.run(self._run(stop_event, on_connected, on_reconnecting))
        except self._client_error as error:
            raise FatalConnectionError(error) from None

    def close(self):
        self._closed = True


def build_sdk_connector(config: FeishuConfig, event_callback):
    return _SDKConnector(config, event_callback)
