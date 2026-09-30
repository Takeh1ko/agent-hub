"""hub status: вся картина для Claude, ≤ 1500 байт по умолчанию."""

from __future__ import annotations

from pathlib import Path

from hub import time as ht
from hub.read import snapshot as snap
from hub.store import FINAL_STAGES, Store

def default_opencode_db() -> Path:
    """Путь чужой БД, HOME читается при вызове (тесты подменяют HOME)."""
    return Path.home() / ".local/share/opencode/opencode.db"


def _worktrees_dirs() -> list[Path]:
    """Каталоги worktrees всех проектов + fallback на .hub.toml в cwd."""
    try:
        from hub.config import load_project, load_projects

        projs = load_projects()
        if not projs:
            try:
                one = load_project(Path.cwd())
            except (FileNotFoundError, OSError):
                one = None
            if one is not None and str(getattr(one, "worktrees", "") or ""):
                projs = [one]
    except Exception as e:
        import sys

        print(f"авто-импорт: нет конфига ({e})", file=sys.stderr)
        return []
    out: list[Path] = []
    for p in projs:
        wt = str(getattr(p, "worktrees", "") or "")
        if wt and Path(wt) not in out:
            out.append(Path(wt))
    return out


def auto_import(store: Store) -> None:
    """Импорт legacy для всех worktrees; ошибки — в stderr, статус не роняем."""
    import sys

    for wt in _worktrees_dirs():
        try:
            store.import_legacy(wt)
        except Exception as e:
            print(f"авто-импорт {wt}: {e}", file=sys.stderr)
            continue


def _snap(args) -> snap.Snapshot:
    store = Store()
    # Авто-импорт legacy при каждом status: без ручного import-legacy.
    auto_import(store)
    db = Path(args.opencode_db) if getattr(args, "opencode_db", None) else default_opencode_db()
    # --all: слитые/брошенные с деньгами и сессиями (include_done), иначе они дешёвые (0).
    s = snap.build(store, ht.now_ms(),
                   opencode_db=str(db) if db.exists() else None,
                   proc_root=getattr(args, "proc_root", "/proc"),
                   include_done=bool(getattr(args, "all", False)))
    if not getattr(args, "all", False):
        s.tasks = [t for t in s.tasks if t.stage not in FINAL_STAGES]
    return s


def cmd_status(args) -> int:
    s = _snap(args)
    if getattr(args, "json", False):
        print(s.to_json())
    else:
        print(s.to_text(include_done=getattr(args, "all", False)))
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("status", help="картина работы агентов (≤1,5 КБ)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="включая merged/dropped")
    p.set_defaults(func=cmd_status)
