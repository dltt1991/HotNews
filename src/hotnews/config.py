import json
import os
import re
from pathlib import Path
from typing import Any, Dict


ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise ValueError("missing environment variable: %s" % name)
            return os.environ[name]
        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_config(path: str) -> Dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        config = _expand(json.load(handle))
    if not config.get("sources"):
        raise ValueError("config.sources cannot be empty")
    if not config.get("subscriptions"):
        raise ValueError("config.subscriptions cannot be empty")
    return config

