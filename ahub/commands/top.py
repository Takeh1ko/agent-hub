"""ahub top — human screen (view; c for control)."""

from __future__ import annotations


def cmd_top(args) -> int:
    from ahub.tui.app import main

    return main(control=args.control)


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("top", help=t("help.top"))
    p.add_argument("--control", action="store_true", help=t("help.top_control"))
    p.set_defaults(func=cmd_top)
