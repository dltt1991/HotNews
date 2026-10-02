"""Stable identities shared by migrations and delivery history."""

import hashlib
import json


def normalized_key(value: str) -> str:
    return " ".join(value.split()).casefold()


def topic_fingerprint(keywords) -> str:
    canonical = json.dumps(sorted({normalized_key(word) for word in keywords}),
                           ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()
