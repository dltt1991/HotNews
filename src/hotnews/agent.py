import logging
from typing import Any, Dict, Iterable, List, Optional

from .channels import render_markdown, send
from .collectors import collect
from .models import NewsItem
from .store import DeliveryStore


LOGGER = logging.getLogger(__name__)


def _matches(item: NewsItem, subscription: Dict[str, Any]) -> bool:
    haystack = (item.title + " " + item.summary).lower()
    includes = [str(word).lower() for word in subscription.get("keywords", [])]
    excludes = [str(word).lower() for word in subscription.get("exclude_keywords", [])]
    return (not includes or any(word in haystack for word in includes)) and not any(
        word in haystack for word in excludes
    ) and item.score >= float(subscription.get("min_score", 0))


class HotNewsAgent:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.store = DeliveryStore(config.get("database", "data/hotnews.db"))

    def gather(self) -> Dict[str, List[NewsItem]]:
        gathered = {}
        for source in self.config["sources"]:
            try:
                gathered[source["name"]] = collect(source)
            except Exception:
                LOGGER.exception("failed to collect source %s", source.get("name"))
                gathered[source["name"]] = []
        return gathered

    def subscriptions(self):
        dynamic = [rule for _, _, rule in self.store.list_subscriptions()]
        return list(self.config["subscriptions"]) + dynamic

    def run_once(self, subscription_names: Optional[Iterable[str]] = None,
                 dry_run: bool = False) -> int:
        selected = set(subscription_names or [])
        gathered = self.gather()
        delivered = 0
        for subscription in self.subscriptions():
            name = subscription["name"]
            if selected and name not in selected:
                continue
            source_names = subscription.get("sources") or list(gathered)
            candidates = [item for source_name in source_names for item in gathered.get(source_name, [])]
            candidates = [item for item in candidates if _matches(item, subscription)]
            candidates.sort(key=lambda item: (item.score, item.published_at or 0), reverse=True)
            candidates = candidates[: int(subscription.get("max_items", 10))]
            for index, channel in enumerate(subscription.get("channels", [])):
                channel_id = channel.get("name") or "%s:%d" % (channel["type"], index)
                pending = [item for item in candidates if not self.store.was_delivered(item.identity, name, channel_id)]
                if not pending:
                    LOGGER.info("no new items for %s -> %s", name, channel_id)
                    continue
                if dry_run:
                    print(render_markdown(subscription.get("title", name), pending))
                    continue
                send(channel, subscription.get("title", name), pending)
                for item in pending:
                    self.store.mark_delivered(item.identity, name, channel_id)
                delivered += len(pending)
                LOGGER.info("delivered %d items: %s -> %s", len(pending), name, channel_id)
        return delivered
