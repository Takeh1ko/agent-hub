"""hub merge: слияние готовой ветки в work_branch."""

from __future__ import annotations

import tomllib

from hub.config import load_project
from hub.pipeline.merge import merge_task
from hub.store import Store


def cmd_merge(args) -> int:
    task_id = args.task_id
    force = bool(getattr(args, "force", False))
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
    ok, msg = merge_task(store, project, task_id, force=force)
    print(msg)
    return 0 if ok else 1


def register(subparsers) -> None:
    p = subparsers.add_parser("merge", help="слить ready в work_branch")
    p.add_argument("task_id", help="ID задачи")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.add_argument("--force", action="store_true", help="слить из arbiter")
    p.set_defaults(func=cmd_merge)
