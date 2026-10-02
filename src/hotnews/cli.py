import argparse
import logging

from .agent import HotNewsAgent
from .config import load_config
from .scheduler import serve
from .server import serve_callbacks


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Hot news subscription agent")
    result.add_argument("--config", default="config.json", help="JSON config file")
    result.add_argument("--verbose", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    once = commands.add_parser("once", help="collect and deliver once")
    once.add_argument("--subscription", action="append", default=[])
    once.add_argument("--dry-run", action="store_true", help="print without sending or recording")
    commands.add_parser("serve", help="run scheduler forever")
    commands.add_parser("webhook", help="run callback HTTP server only")
    return result


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    if args.command == "once":
        HotNewsAgent(config).run_once(args.subscription, args.dry_run)
    elif args.command == "serve":
        serve(config)
    else:
        serve_callbacks(config)


if __name__ == "__main__":
    main()
