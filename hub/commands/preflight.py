"""hub preflight: проверка окружения. Тонкая обёртка над hub.gate.preflight."""

from __future__ import annotations

import sqlite3
import tomllib

from hub.config import load_project
from hub.gate.preflight import preflight
from hub.store import Store


def cmd_preflight(args) -> int:
    task_id = args.task_id
    proj_src = getattr(args, "project", None) or "."
    try:
        project = load_project(proj_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        print(f"FAIL {task_id} no-project")
        return 1
    try:
        store = Store()
    except (OSError, sqlite3.Error) as e:
        print(f"FAIL {task_id} store-fail: {e}")
        return 1
    res = preflight(store, task_id, project)
    if res.ok:
        print(f"OK {task_id}")
        return 0
    print(f"FAIL {task_id} {res.reason}")
    return 1


def register(subparsers) -> None:
    p = subparsers.add_parser("preflight", help="проверить окружение задачи")
    p.add_argument("task_id", help="id задачи в store")
    p.add_argument("--project", default=None, help="корень проекта (поиск .hub.toml)")
    p.set_defaults(func=cmd_preflight)
