import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict

from .agent import HotNewsAgent


LOGGER = logging.getLogger(__name__)


def _due(schedule: Dict[str, Any], now: datetime, state: Dict[str, float], name: str) -> bool:
    kind = schedule.get("type", "interval")
    last = state.get(name, 0)
    if kind == "interval":
        return now.timestamp() - last >= float(schedule.get("minutes", 30)) * 60
    if kind == "daily":
        target = schedule.get("at", "09:00")
        current_key = now.strftime("%Y-%m-%d %H:%M")
        return now.strftime("%H:%M") == target and state.get(name) != current_key
    raise ValueError("unsupported schedule type: %s" % kind)


def serve(config: Dict[str, Any]) -> None:
    if config.get("inbound", {}).get("enabled", False):
        from .server import serve_callbacks
        threading.Thread(target=serve_callbacks, args=(config,), daemon=True).start()
    agent = HotNewsAgent(config)
    state: Dict[str, Any] = {}
    poll_seconds = int(config.get("scheduler", {}).get("poll_seconds", 30))
    LOGGER.info("scheduler started; poll interval=%ss", poll_seconds)
    while True:
        now = datetime.now()
        for subscription in agent.subscriptions():
            name = subscription["name"]
            try:
                if _due(subscription.get("schedule", {}), now, state, name):
                    agent.run_once([name])
                    schedule_type = subscription.get("schedule", {}).get("type", "interval")
                    state[name] = now.strftime("%Y-%m-%d %H:%M") if schedule_type == "daily" else now.timestamp()
            except Exception:
                LOGGER.exception("subscription run failed: %s", name)
        time.sleep(poll_seconds)
