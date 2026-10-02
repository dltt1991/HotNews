import argparse
from datetime import datetime, timezone
import json
import logging
import sys

from .commands.schema import nonempty_string, object_fields, parse_intent, parse_json, positive_integer
from .commands.service import CommandService, json_default
from .config import load_config
from .domain import LeaseConflict, ValidationError, VersionConflict
from .storage.database import Database
from .storage.events import EventRepository


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
    once = commands.add_parser("once", help="collect and deliver once")
    once.add_argument("--subscription", action="append", default=[])
    once.add_argument("--dry-run", action="store_true", help="print without sending or recording")
    commands.add_parser("serve", help="run scheduler forever")
    commands.add_parser("webhook", help="run callback HTTP server only")
    agent = commands.add_parser("agent", help="restricted JSON interface for Codex")
    actions = agent.add_subparsers(dest="agent_command", required=True)
    actions.add_parser("claim-events", help="claim a bounded batch of queued group messages")
    actions.add_parser("apply-intent", help="validate and atomically apply one structured command")
    actions.add_parser("fail-event", help="mark one owned event as failed")
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
    else:
        raise ValidationError("unsupported agent command")
    database = Database(config.database_path)
    database.migrate()
    events = EventRepository(database)
    now = datetime.now(timezone.utc)
    if action == "claim-events":
        return {"events": events.claim_pending(owner, limit, now, config.worker.lease_seconds)}
    if action == "apply-intent":
        return {"result": CommandService(database).apply(event_id, owner, intent)}
    # Model-provided diagnostics may contain message text or credentials. Keep
    # only a fixed safe reason in durable state, never their untrusted content.
    events.fail(event_id, owner, "command processing failed", now=now)
    return {"event_id": event_id, "status": "failed"}


def main() -> int:
    args = parser().parse_args()
    if args.command == "agent":
        try:
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
    config = load_config(args.config)
    if args.command == "once":
        from .agent import HotNewsAgent
        HotNewsAgent(config).run_once(args.subscription, args.dry_run)
    elif args.command == "serve":
        from .scheduler import serve
        serve(config)
    else:
        from .server import serve_callbacks
        serve_callbacks(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
