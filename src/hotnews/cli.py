import argparse
from datetime import datetime, timezone
import json
import logging
import sys

from .commands.schema import nonempty_string, object_fields, parse_intent, parse_json, positive_integer
from .commands.service import CommandService, json_default
from .config import load_config
from .domain import LeaseConflict, NewsResult, Schedule, Subscription, ValidationError, VersionConflict
from .feishu.cards import render_digest
from .storage.database import Database
from .storage.events import EventRepository, LeaseRepository, _datetime, _utc_text
from .storage.runs import RunRepository
from .storage.subscriptions import SubscriptionRepository


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default includes unrecognized argument values. The CLI's
        # diagnostics must not echo potentially sensitive caller input.
        write_output({"error": "validation_error"})
        sys.stderr.write("Invalid command-line arguments.\n")
        raise SystemExit(2)


def parser() -> argparse.ArgumentParser:
    result = ArgumentParser(description="Hot news subscription agent")
    result.add_argument("--config", default="config.json", help="JSON config file")
    result.add_argument("--verbose", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run Feishu callback, localhost admin and outbox worker")
    agent = commands.add_parser("agent", help="restricted JSON interface for Codex")
    actions = agent.add_subparsers(dest="agent_command", required=True)
    actions.add_parser("claim-events", help="claim a bounded batch of queued group messages")
    actions.add_parser("apply-intent", help="validate and atomically apply one structured command")
    actions.add_parser("fail-event", help="mark one owned event as failed")
    actions.add_parser("defer-event", help="release one unfinished owned event for the next tick")
    for action, help_text in (
        ("acquire-run-lease", "acquire the global Codex workflow lease"),
        ("renew-run-lease", "renew an unexpired global lease"),
        ("release-run-lease", "release an owned global lease"),
        ("claim-term-refresh", "claim subscriptions needing new search terms"),
        ("complete-term-refresh", "save validated terms for a leased subscription version"),
        ("fail-term-refresh", "release unsuccessful term refresh work"),
        ("claim-due", "claim a bounded batch of scheduled or manual runs"),
        ("list-due", "preview eligible subscriptions without leasing"),
        ("history", "read confirmed article history for a subscription topic"),
        ("complete-run", "prepare validated news for durable delivery"),
        ("fail-run", "fail owned research work and update its failure counter"),
    ):
        actions.add_parser(action, help=help_text)
    commands.add_parser("dry-run", help="render validated news JSON as a card without state changes or sends")
    return result


def read_input():
    """Read one bounded JSON document, never interpreting input as code."""
    try:
        raw = sys.stdin.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValidationError("agent input is too large")
        return parse_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ValidationError("invalid agent JSON input") from None


def write_output(value):
    sys.stdout.write(json.dumps(value, default=json_default, ensure_ascii=True, allow_nan=False) + "\n")


def parse_news_results(value):
    """Dates must be explicit aware timestamps; unknown result fields fail closed."""
    if not isinstance(value, list) or len(value) > 10:
        raise ValidationError("results must be an array of at most ten items")
    results = []
    for item in value:
        object_fields(item, ("title", "url", "source", "published_at", "summary", "event_key"), ("references",))
        for name in ("title", "url", "source", "published_at", "summary", "event_key"):
            nonempty_string(item[name], name)
        try:
            published = _datetime(item["published_at"])
            _utc_text(published)
        except (ValueError, TypeError):
            raise ValidationError("publication date must be a timezone-aware timestamp") from None
        references = item.get("references", [])
        if not isinstance(references, list) or len(references) > 2:
            raise ValidationError("references must contain at most two URLs")
        results.append(NewsResult(
            title=item["title"], url=item["url"], source=item["source"], published_at=published,
            summary=item["summary"], event_key=item["event_key"],
            references=tuple(nonempty_string(reference, "reference") for reference in references),
        ))
    return results


def run_dry_run(value):
    """Render a supplied research preview without opening any application state."""
    object_fields(value, ("subscription", "results", "search_window_days"))
    context = value["subscription"]
    object_fields(context, ("display_number", "topic", "keywords"))
    number = positive_integer(context["display_number"], "display_number")
    intent = parse_intent({"action": "create_subscription", "topic": context["topic"],
                           "keywords": context["keywords"]})
    now = datetime.now(timezone.utc)
    window = value["search_window_days"]
    results = RunRepository._results(parse_news_results(value["results"]), now, window)
    selected, urls, events = [], set(), set()
    for item in results:
        if item.url not in urls and item.event_key not in events:
            selected.append(item)
            urls.add(item.url)
            events.add(item.event_key)
    subscription = Subscription("preview", "preview", number, "preview", intent.topic, intent.keywords,
                                (), Schedule("manual"), "ready", None, 1, now, now)
    return {"card": render_digest(subscription, selected, window, now=now) if selected else None,
            "result_count": len(selected), "search_window_days": window}


def run_agent(action, config, value):
    """Envelope validation precedes migration or queue mutation."""
    if action == "claim-events":
        object_fields(value, ("owner",), ("limit",))
        owner = nonempty_string(value["owner"], "owner")
        limit = positive_integer(value.get("limit", config.worker.max_queued_events), "limit")
        if limit > config.worker.max_queued_events:
            raise ValidationError("claim limit exceeds configured event batch")
    elif action == "apply-intent":
        object_fields(value, ("event_id", "owner", "intent"))
        owner = nonempty_string(value["owner"], "owner")
        event_id = nonempty_string(value["event_id"], "event_id")
        intent = parse_intent(value["intent"])
    elif action == "fail-event":
        object_fields(value, ("event_id", "owner", "error"))
        owner = nonempty_string(value["owner"], "owner")
        event_id = nonempty_string(value["event_id"], "event_id")
        nonempty_string(value["error"], "error")
    elif action == "defer-event":
        object_fields(value, ("event_id", "owner"))
        owner = nonempty_string(value["owner"], "owner")
        event_id = nonempty_string(value["event_id"], "event_id")
    elif action in ("acquire-run-lease", "renew-run-lease", "release-run-lease"):
        object_fields(value, ("owner",))
        owner = nonempty_string(value["owner"], "owner")
    elif action in ("claim-due", "claim-term-refresh", "list-due"):
        object_fields(value, () if action == "list-due" else ("owner",), ("limit",))
        if action != "list-due":
            owner = nonempty_string(value["owner"], "owner")
        limit = positive_integer(value.get("limit", config.worker.max_due_subscriptions), "limit")
        if limit > config.worker.max_due_subscriptions:
            raise ValidationError("claim limit exceeds configured subscription batch")
    elif action in ("complete-term-refresh", "fail-term-refresh"):
        object_fields(value, ("subscription_id", "owner", "expected_version",
                              "terms" if action == "complete-term-refresh" else "error"))
        subscription_id = nonempty_string(value["subscription_id"], "subscription_id")
        owner = nonempty_string(value["owner"], "owner")
        version = positive_integer(value["expected_version"], "expected_version")
        if action == "complete-term-refresh":
            terms = value["terms"]
            if not isinstance(terms, list) or not terms:
                raise ValidationError("terms must be a non-empty array")
            terms = [nonempty_string(term, "term") for term in terms]
        else:
            nonempty_string(value["error"], "error")
    elif action == "history":
        object_fields(value, ("subscription_id",))
        subscription_id = nonempty_string(value["subscription_id"], "subscription_id")
    elif action in ("complete-run", "fail-run"):
        object_fields(value, ("run_id", "owner", "results" if action == "complete-run" else "error"),
                      ("search_window_days",) if action == "complete-run" else ())
        run_id = nonempty_string(value["run_id"], "run_id")
        owner = nonempty_string(value["owner"], "owner")
        if action == "complete-run":
            results = parse_news_results(value["results"])
            window = value.get("search_window_days", 30)
            if type(window) is not int or window not in (1, 7, 30):
                raise ValidationError("search window must be 1, 7 or 30 days")
        else:
            nonempty_string(value["error"], "error")
    else:
        raise ValidationError("unsupported agent command")
    if action in ("list-due", "history"):
        runs = RunRepository(Database(config.database_path, read_only=True))
        if action == "list-due":
            return {"subscriptions": runs.list_due(datetime.now(timezone.utc), limit)}
        return {"history": runs.history(subscription_id)}
    database = Database(config.database_path)
    database.migrate()
    events = EventRepository(database)
    now = datetime.now(timezone.utc)
    if action == "claim-events":
        return {"events": events.claim_pending(owner, limit, now, config.worker.lease_seconds)}
    if action == "apply-intent":
        return {"result": CommandService(database).apply(event_id, owner, intent)}
    if action == "defer-event":
        events.defer(event_id, owner, now=now)
        return {"event_id": event_id, "status": "pending"}
    if action == "acquire-run-lease":
        return {"acquired": LeaseRepository(database).acquire("hotnews-agent", owner, now, config.worker.lease_seconds)}
    if action == "renew-run-lease":
        return {"renewed": LeaseRepository(database).renew("hotnews-agent", owner, now, config.worker.lease_seconds)}
    if action == "release-run-lease":
        return {"released": LeaseRepository(database).release("hotnews-agent", owner)}
    subscriptions = SubscriptionRepository(database)
    if action == "claim-term-refresh":
        return {"subscriptions": subscriptions.claim_pending_terms(owner, limit, now, config.worker.lease_seconds)}
    if action == "complete-term-refresh":
        return {"subscription": subscriptions.complete_search_terms(subscription_id, owner, version, terms, now=now)}
    if action == "fail-term-refresh":
        updated = subscriptions.fail_search_terms(subscription_id, owner, version, "term refresh failed", now=now)
        sys.stderr.write("Search-term refresh failed; work released for retry.\n")
        return {"subscription": updated}
    runs = RunRepository(database)
    if action == "claim-due":
        claimed = runs.claim_due(owner, limit, now, config.worker.lease_seconds)
        return {"runs": [{"run": run, "subscription": subscriptions.get(run.subscription_id)} for run in claimed]}
    if action == "complete-run":
        return {"run": runs.complete(run_id, owner, results, now=now, search_window_days=window)}
    if action == "fail-run":
        return {"run": runs.fail(run_id, owner, "news research failed", now=now)}
    # Model-provided diagnostics may contain message text or credentials. Keep
    # only a fixed safe reason in durable state, never their untrusted content.
    events.fail(event_id, owner, "command processing failed", now=now)
    return {"event_id": event_id, "status": "failed"}


def main() -> int:
    args = parser().parse_args()
    if args.command in ("agent", "dry-run"):
        try:
            if args.command == "dry-run":
                result = run_dry_run(read_input())
            else:
                config = load_config(args.config)
                result = run_agent(args.agent_command, config, read_input())
            write_output(result)
            return 0
        except (ValidationError, LeaseConflict, VersionConflict, OverflowError):
            write_output({"error": "validation_error"})
            sys.stderr.write("Invalid agent input, state, or lease.\n")
            return 2
        except Exception:
            write_output({"error": "internal_error"})
            sys.stderr.write("Agent operation failed.\n")
            return 1
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    import signal
    import threading
    from .runtime import run_service
    stop_event = threading.Event()
    original_handlers = {}
    try:
        config = load_config(args.config)
        for signum in (signal.SIGINT, signal.SIGTERM):
            original_handlers[signum] = signal.signal(signum, lambda *_: stop_event.set())
        run_service(config, stop_event)
        return 0
    except Exception:
        sys.stderr.write("Runtime startup or service failed; check configuration and service logs.\n")
        return 1
    finally:
        stop_event.set()
        for signum, handler in original_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
