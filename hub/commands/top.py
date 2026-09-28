"""hub top: живой экран задач."""

from __future__ import annotations

import sys
from pathlib import Path

DEFAULT_OPENCODB = Path.home() / ".local/share/opencode/opencode.db"


def cmd_top(args) -> int:
    if not sys.stdout.isatty():
        print("нужен терминал", file=sys.stderr)
        return 3
    from hub.tui.app import HubApp

    # Как в hub status: живой top обязан видеть сессии/деньги,
    # иначе snapshot.build при opencode_db=None даёт нули.
    db = Path.home() / ".local/share/opencode/opencode.db"
    app = HubApp(opencode_db=str(db) if db.exists() else None)
    proj = getattr(args, "project", None)
    if proj:
        app.project_filter = proj
    app.run()
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("top", help="живой экран задач")
    p.add_argument("--project", default=None, help="фильтр таблицы по проекту (ROOT)")
    p.set_defaults(func=cmd_top)
