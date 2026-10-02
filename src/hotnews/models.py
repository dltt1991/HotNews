from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    url: str
    summary: str = ""
    score: float = 0
    published_at: Optional[datetime] = None

    @property
    def identity(self) -> str:
        import hashlib

        raw = (self.url.strip() or self.title.strip()).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @property
    def published_text(self) -> str:
        if not self.published_at:
            return ""
        value = self.published_at
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone().strftime("%m-%d %H:%M")

