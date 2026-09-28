"""hub status: вся картина для Claude, ≤ 1500 байт по умолчанию."""

from __future__ import annotations

from pathlib import Path

from hub import time as ht
from hub.read import snapshot as snap
from hub.store import FINAL_STAGES, Store

DEFAULT_OPENCODB = Path.home() / ".local/share/opencode/opencode.db"


def _snap(args) -> snap.Snapshot:
    store = Store()
    db = Path(args.opencode_db) if getattr(args, "opencode_db", None) else DEFAULT_OPENCODB
    s = snap.build(store, ht.now_ms(),
                   opencode_db=str(db) if db.exists() else None,
                   proc_root=getattr(args, "proc_root", "/proc"))
    if not getattr(args, "all", False):
        s.tasks = [t for t in s.tasks if t.stage not in FINAL_STAGES]
    return s


def cmd_status(args) -> int:
    s = _snap(args)
    if getattr(args, "json", False):
        print(s.to_json())
    else:
        print(s.to_text())
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("status", help="картина работы агентов (≤1,5 КБ)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="включая merged/dropped")
    p.set_defaults(func=cmd_status)
