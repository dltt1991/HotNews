"""Validate long-connection proxies and sanitize network diagnostics."""

import re
from typing import Mapping, Optional
from urllib.parse import urlsplit

from ..domain import ValidationError


_PROXY_NAMES = ("FEISHU_WS_PROXY", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")
_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}
_URL = re.compile(r"(?i)\b(?:https?|socks5h?|wss)://[^\s;]+")


def _validate_proxy(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValidationError("invalid Feishu proxy URL") from None
    if parsed.scheme.lower() not in _PROXY_SCHEMES or not parsed.hostname or port is None:
        raise ValidationError("invalid Feishu proxy URL")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValidationError("invalid Feishu proxy URL")
    return value


def resolve_ws_proxy(environ: Mapping[str, str]) -> Optional[str]:
    """Return the first configured, valid proxy without modifying the environment."""
    for name in _PROXY_NAMES:
        value = environ.get(name, "").strip()
        if value:
            return _validate_proxy(value)
    return None


def _redact_url(match) -> str:
    value = match.group(0)
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return "<redacted-url>"
    host = parsed.hostname or "<redacted-host>"
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    authority = host + ((":" + str(port)) if port is not None else "")
    if parsed.username is not None or parsed.password is not None:
        authority = "***:***@" + authority
    path = parsed.path or ""
    query = "?<redacted>" if parsed.query else ""
    return "%s://%s%s%s" % (parsed.scheme, authority, path, query)


def redact_sensitive(value: object) -> str:
    """Remove URL credentials and temporary query values from an error string."""
    return _URL.sub(_redact_url, str(value))
