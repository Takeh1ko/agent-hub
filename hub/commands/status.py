"""hub status: вся картина для Claude, ≤ 1500 байт по умолчанию."""

from __future__ import annotations

from pathlib import Path

from hub import time as ht
from hub.read import snapshot as snap
from hub.store import FINAL_STAGES, Store

def default_opencode_db() -> Path:
    """Путь чужой БД, HOME читается при вызове (тесты подменяют HOME)."""
    return Path.home() / ".local/share/opencode/opencode.db"


def _worktrees_dir() -> Path | None:
    """Каталог worktrees из первого проекта, None если нет."""
    try:
        from hub.config import load_projects

        projs = load_projects()
    except Exception:
        return None
    if not projs:
        return None
    wt = str(getattr(projs[0], "worktrees", "") or "")
    return Path(wt) if wt else None


def _snap(args) -> snap.Snapshot:
    store = Store()
    # Авто-импорт legacy при каждом status: без ручного import-legacy.
    # Не роняет статус при битом конфиге/каталоге.
    try:
        wt = _worktrees_dir()
        if wt is not None and wt.is_dir():
            store.import_legacy(wt)
        else:
            # Даже без каталога — снять висящие задачи со снятым worktree.
            try:
                store.import_legacy(wt if wt is not None else "")
            except Exception:
                pass
    except Exception:
        pass
    db = Path(args.opencode_db) if getattr(args, "opencode_db", None) else default_opencode_db()
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
