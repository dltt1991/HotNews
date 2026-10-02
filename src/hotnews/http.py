import json
from typing import Any, Dict, Optional
from urllib.request import Request, urlopen


USER_AGENT = "hotnews-agent/0.1 (+https://localhost)"


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
