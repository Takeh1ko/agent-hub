"""hub top: живой экран задач."""

from __future__ import annotations

import sys


def cmd_top(args) -> int:
    if not sys.stdout.isatty():
        print("нужен терминал", file=sys.stderr)
        return 3
    from hub.tui.app import HubApp

    app = HubApp()
    proj = getattr(args, "project", None)
    if proj:
        app.project_filter = proj
    app.run()
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("top", help="живой экран задач")
    p.add_argument("--project", default=None, help="фильтр таблицы по проекту (ROOT)")
    p.set_defaults(func=cmd_top)
