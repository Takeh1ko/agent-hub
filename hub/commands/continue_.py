"""hub continue: новые «Решения арбитра» → та же ветка, база = merge-base."""

from __future__ import annotations

import subprocess
import time
import tomllib
from pathlib import Path

from hub.config import load_project
from hub.pipeline.common import merge_base
from hub.store import Store


def cmd_continue(args) -> int:
    task_id = args.task_id
    store = Store()
    task = store.get_task(task_id)
    if task is None:
        print(f"no-task: {task_id}")
        return 1
    proj_src = getattr(args, "project", None) or str(task.get("worktree") or ".")
    try:
        project = load_project(proj_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"FAIL {task_id} no-project: {e}")
        return 1
    worktree = str(task.get("worktree") or "")
    branch = str(task.get("branch") or f"agent/{task_id}")
    root = str(getattr(project, "root", "") or "")
    if not worktree or not Path(worktree).is_dir():
        print(f"no-worktree: {worktree or '?'}")
        return 1
    if not root:
        print("no-project: нет root")
        return 1
    work_branch = (getattr(project, "work_branch", "") or "").strip()
    base_ref = work_branch or "HEAD"
    # База = merge-base рабочей ветки и ветки задачи.
    base = merge_base(root, base_ref, branch)
    if not base:
        print(f"no-merge-base: {base_ref} {branch}")
        return 1
    # Старое .agent → .agent.prev_<ts>.
    agent = Path(worktree) / ".agent"
    if agent.exists():
        prev = Path(worktree) / f".agent.prev_{int(time.time())}"
        try:
            agent.rename(prev)
        except OSError as e:
            print(f"agent-rename-fail: {e}")
            return 1
    Path(worktree, ".agent").mkdir(parents=True, exist_ok=True)
    try:
        store.upsert_task(id=task_id, base_sha=base, stage="queued",
                          round=0, stage_reason="continue: новые решения арбитра")
        store.add_event(task_id, "stage", {"stage": "queued", "why": "continue",
                                           "base": base})
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    # Флаг продолжения: HEAD ветки уже впереди базы (работа прошлого
    # исполнителя), штатный preflight (HEAD == base) к ней неприменим —
    # конвейер проверит merge-base вместо HEAD.
    try:
        from hub.pipeline.common import meta_set

        meta_set(store, f"continued:{task_id}", "1")
    except (OSError, ValueError):
        pass
    print(f"OK {task_id} base={base[:8]}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("continue", help="продолжить задачу после решений арбитра")
    p.add_argument("task_id", help="ID задачи")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.set_defaults(func=cmd_continue)
