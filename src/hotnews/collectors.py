import calendar
import html
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional
from xml.etree import ElementTree

from .http import get_bytes, get_json
from .models import NewsItem


def _text(element: ElementTree.Element, names: Iterable[str]) -> str:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return html.unescape(child.text.strip())
    return ""


def _date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None


def collect_rss(source: Dict[str, Any]) -> List[NewsItem]:
    root = ElementTree.fromstring(get_bytes(source["url"], int(source.get("timeout", 15))))
    entries = root.findall(".//item")
    atom = False
    if not entries:
        entries = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        atom = True
    result = []
    for entry in entries[: int(source.get("limit", 30))]:
        if atom:
            prefix = "{http://www.w3.org/2005/Atom}"
            title = _text(entry, [prefix + "title"])
            summary = _text(entry, [prefix + "summary", prefix + "content"])
            published = _text(entry, [prefix + "published", prefix + "updated"])
            link_node = entry.find(prefix + "link")
            url = link_node.get("href", "") if link_node is not None else ""
        else:
            title = _text(entry, ["title"])
            summary = _text(entry, ["description", "summary"])
            published = _text(entry, ["pubDate", "date"])
            url = _text(entry, ["link", "guid"])
        if title and url:
            result.append(NewsItem(source=source["name"], title=title, url=url,
                                   summary=summary, published_at=_date(published)))
    return result


def collect_hackernews(source: Dict[str, Any]) -> List[NewsItem]:
    api = source.get("api", "https://hacker-news.firebaseio.com/v0")
    limit = int(source.get("limit", 20))
    ids = get_json(api + "/topstories.json", int(source.get("timeout", 15)))[:limit]
    result = []
    for item_id in ids:
        item = get_json("%s/item/%s.json" % (api, item_id), int(source.get("timeout", 15)))
        if not item or item.get("type") != "story" or not item.get("title"):
            continue
        url = item.get("url") or "https://news.ycombinator.com/item?id=%s" % item_id
        published = datetime.fromtimestamp(item["time"], timezone.utc) if item.get("time") else None
        result.append(NewsItem(source=source["name"], title=item["title"], url=url,
                               score=float(item.get("score", 0)), published_at=published))
    return result


def collect(source: Dict[str, Any]) -> List[NewsItem]:
    kind = source.get("type", "rss")
    if kind in ("rss", "atom"):
        return collect_rss(source)
    if kind == "hackernews":
        return collect_hackernews(source)
    raise ValueError("unsupported source type: %s" % kind)

