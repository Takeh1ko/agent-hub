"""hub stop: кооперативно (файл), жёстко --kill (best-effort)."""

from __future__ import annotations

import signal
import tomllib
from pathlib import Path

from hub.config import load_project
from hub.store import Store


def cmd_stop(args) -> int:
    task_id = args.task_id
    hard = bool(getattr(args, "kill", False))
    store = Store()
    task = store.get_task(task_id)
    if task is None:
        print(f"no-task: {task_id}")
        return 1
    if str(task.get("stage") or "") in ("merged", "dropped"):
        print(f"not-stop: {task_id} уже {task.get('stage')}")
        return 1
    worktree = str(task.get("worktree") or "")
    if worktree:
        try:
            Path(worktree, ".agent").mkdir(parents=True, exist_ok=True)
            (Path(worktree) / ".agent" / "stop_requested").write_text(
                "stop\n", encoding="utf-8")
        except OSError:
            pass
    if hard:
        # Жёстко: пробуем SIGTERM процессам из worktree (best-effort, чужие не трогаем).
        try:
            from hub.read import procs as _pr

            for p in _pr.agent_procs("/proc"):
                if worktree and (p.cwd == worktree or p.cwd.startswith(worktree + "/")):
                    try:
                        import os as _os

                        _os.kill(p.pid, signal.SIGTERM)
                    except (OSError, PermissionError):
                        continue
        except OSError:
            pass
    try:
        store.upsert_task(id=task_id, stage="stopped", stage_reason="stop владельца")
        store.add_event(task_id, "stage", {"stage": "stopped", "why": "stop",
                                           "kill": hard})
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    print(f"OK {task_id} stopped")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("stop", help="остановить задачу (кооперативно)")
    p.add_argument("task_id", help="ID задачи")
    p.add_argument("--kill", action="store_true", help="жёстко SIGTERM")
    p.set_defaults(func=cmd_stop)
