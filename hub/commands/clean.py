"""hub clean: осиротевшие worktree/ветки без задачи (по умолчанию — показать)."""

from __future__ import annotations

import subprocess
import tomllib

from hub.config import load_project
from hub.pipeline.merge import list_orphans
from hub.store import Store


def cmd_clean(args) -> int:
    proj_src = getattr(args, "project", None) or "."
    try:
        project = load_project(proj_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"нет проекта: {e}")
        return 1
    store = Store()
    orph_wt, orph_br = list_orphans(project, store)
    if not orph_wt and not orph_br:
        print("(чисто)")
        return 0
    for w in orph_wt:
        print(f"worktree: {w.get('path')}")
    for b in orph_br:
        print(f"branch: {b}")
    if not bool(getattr(args, "yes", False)):
        print("показано без удаления (дайте --yes)")
        return 0
    root = str(getattr(project, "root", "") or "")
    failed: list[str] = []
    for w in orph_wt:
        path = str(w.get("path") or "")
        try:
            r = subprocess.run(["git", "worktree", "remove", "--force", path],
                               cwd=root, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            failed.append(f"worktree {path}: {e}")
            continue
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or f"код {r.returncode}")
            failed.append(f"worktree {path}: {err}")
    for b in orph_br:
        try:
            r = subprocess.run(["git", "branch", "-D", b], cwd=root,
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            failed.append(f"branch {b}: {e}")
            continue
        if r.returncode != 0:
            err = (r.stderr.strip() or r.stdout.strip() or f"код {r.returncode}")
            failed.append(f"branch {b}: {err}")
    if failed:
        for f in failed:
            print(f"fail: {f}")
        return 1
    print("OK clean")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("clean", help="осиротевшие worktree/ветки")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.add_argument("--yes", action="store_true", help="удалить")
    p.set_defaults(func=cmd_clean)
