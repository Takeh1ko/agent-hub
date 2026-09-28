"""hub findings: сжатые замечания ревью по задаче."""

from __future__ import annotations

import sys
from pathlib import Path

from hub.read.findings import dedup_findings, format_findings, load_findings
from hub.store import Store


def cmd_findings(args) -> int:
    store = Store()
    task = store.get_task(args.id)
    if task is None:
        print(f"нет задачи {args.id}", file=sys.stderr)
        return 1
    wt = str(task.get("worktree") or "")
    items = load_findings(Path(wt), getattr(args, "round", None)) if wt else []
    # --fix: та же дедуп-печать для починки, файлы не правим.
    text = format_findings(dedup_findings(items))
    if text:
        print(text)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("findings", help="замечания ревью (дедуп)")
    p.add_argument("id", help="ID задачи")
    p.add_argument("--round", type=int, default=None, help="круг ревью")
    p.add_argument("--fix", action="store_true",
                   help="печать для починки (то же, что без флага: только печать)")
    p.set_defaults(func=cmd_findings)
