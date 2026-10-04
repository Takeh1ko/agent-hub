"""ahub top — the interactive console (same app as bare `ahub` on a TTY)."""

from __future__ import annotations


def cmd_top(args) -> int:
    from ahub import scope
    from ahub.tui.console import main

    sc = scope.resolve(args)
    return main(all_projects=sc.all, project=sc.name or None, control=bool(getattr(args, "control", False)))


def register(subparsers) -> None:
    from ahub.cliutil import add_scope_args
    from ahub.i18n import t

    p = subparsers.add_parser("top", help=t("help.top"))
    p.add_argument("--control", action="store_true", help=t("help.top_control"))
    add_scope_args(p)
    p.set_defaults(func=cmd_top)
