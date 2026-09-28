"""hub import-legacy: задачи старого конвейера (run_task.py) в hub.db."""

from __future__ import annotations

from hub.config import load_projects
from hub.store import Store


def cmd_import_legacy(args) -> int:
    worktrees = getattr(args, "worktrees", None)
    if not worktrees:
        projects = load_projects()
        if not projects or not projects[0].worktrees:
            print("укажи --worktrees DIR")
            return 2
        worktrees = projects[0].worktrees
    ids = Store().import_legacy(worktrees)
    print(f"импортировано: {len(ids)} ({', '.join(ids[:10])})")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("import-legacy", help="импорт .agent/state.json в hub.db")
    p.add_argument("--worktrees", default=None)
    p.set_defaults(func=cmd_import_legacy)
