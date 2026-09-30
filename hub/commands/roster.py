"""hub roster: кто в какой роли над чем."""

from __future__ import annotations

from hub.commands.status import _snap


def cmd_roster(args) -> int:
    print(_snap(args).roster_text(include_done=bool(getattr(args, "all", False))))
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("roster", help="кто, в какой роли, над чем")
    p.add_argument("--all", action="store_true", help="включая merged/dropped")
    p.set_defaults(func=cmd_roster)
