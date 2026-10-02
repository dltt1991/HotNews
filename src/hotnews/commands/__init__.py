"""Validated commands; legacy imports remain until runtime migration completes."""

from .legacy import handle_command, parse_command

__all__ = ["handle_command", "parse_command"]
