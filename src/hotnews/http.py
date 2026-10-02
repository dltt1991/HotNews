import sys
import socket
import threading
import re
from typing import Mapping, Optional, Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .domain import HttpResponse


USER_AGENT = "hotnews-agent/0.1 (+https://localhost)"


class ContentLengthError(ValueError):
    def __init__(self, status: int):
        self.status = status
        super().__init__("invalid or excessive Content-Length")


def bounded_content_length(value: str, maximum: int) -> int:
    """Validate framing before integer conversion on all supported Pythons."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise ContentLengthError(400)
    # Even a 64-bit body length fits in twenty decimal digits. Reject excess
    # padding too, so untrusted headers never reach Python's digit-limit path.
    if len(value) > 20:
        raise ContentLengthError(413)
    size = int(value)
    if size > maximum:
        raise ContentLengthError(413)
    return size


def supervise_request_errors(server, stop_event: threading.Event) -> threading.Event:
    """Route handler-thread failures to the service without raw traceback logs."""
    failed = threading.Event()

    def handle_error(request, client_address):
        # Broken pipes/timeouts are ordinary connection failures, not a crashed
        # service. All other handler errors must reach the owning runtime.
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError, socket.timeout)):
            return
        failed.set()
        stop_event.set()

    server.handle_error = handle_error
    return failed


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
