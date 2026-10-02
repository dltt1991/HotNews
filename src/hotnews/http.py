import json
from typing import Any, Dict, Mapping, Optional, Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .domain import HttpResponse


USER_AGENT = "hotnews-agent/0.1 (+https://localhost)"


class HttpTransport(Protocol):
    """Small synchronous HTTP boundary; callers own JSON and retry policy."""

    def request(self, method: str, url: str, headers: Optional[Mapping[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 15) -> HttpResponse:
        ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Return HTTP failures as responses without forwarding credentials on redirects."""

    def request(self, method: str, url: str, headers: Optional[Mapping[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 15) -> HttpResponse:
        request = Request(url, data=body, method=method,
                          headers={"User-Agent": USER_AGENT, **dict(headers or {})})
        opener = build_opener(_NoRedirect())
        try:
            with opener.open(request, timeout=timeout) as response:
                return HttpResponse(response.status, dict(response.headers.items()), response.read())
        except HTTPError as error:
            with error:
                return HttpResponse(error.code, dict(error.headers.items()), error.read())


def get_bytes(url: str, timeout: int = 15) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def get_json(url: str, timeout: int = 15) -> Any:
    return json.loads(get_bytes(url, timeout).decode("utf-8"))


def post_json(url: str, payload: Dict[str, Any], timeout: int = 15,
              headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}
