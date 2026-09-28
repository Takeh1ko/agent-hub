"""hub queue: воркер очереди (queued → конвейер)."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tomllib

from hub.config import load_project
from hub.pipeline.common import card_text_of, card_network_is_playerok, is_paused, read_extra
from hub.store import Store


def _eligible(store: Store, task: dict, project) -> tuple[bool, str]:
    if str(task.get("stage") or "") != "queued":
        return False, "не queued"
    _, after, _ = read_extra(task)
    if after:
        dep = store.get_task(after)
        if dep is None:
            return False, f"after: нет {after}"
        if str(dep.get("stage") or "") not in ("merged", "dropped", "ready"):
            return False, f"after: ждём {after}"
    if is_paused(store):
        return False, "пауза"
    return True, ""


def _owner_commands(store: Store) -> None:
    """События owner_command (H05 /stop /merge): исполнить до задач."""
    try:
        rows = store.events_since(0)
    except (OSError, sqlite3.Error, ValueError):
        return
    for e in rows:
        if str(e.get("kind") or "") != "owner_command":
            continue
        try:
            payload = json.loads(e.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        cmd = str(payload.get("cmd") or payload.get("action") or "")
        tid = str(payload.get("task_id") or payload.get("id") or "")
        if not tid:
            continue
        if cmd == "stop":
            from hub.commands import stop as stop_mod

            ns = type("A", (), {"task_id": tid, "kill": False})()
            try:
                stop_mod.cmd_stop(ns)
            except (OSError, sqlite3.Error, ValueError):
                pass
        elif cmd == "merge":
            from hub.commands import merge as merge_mod

            ns = type("A", (), {"task_id": tid, "force": False, "project": None})()
            try:
                merge_mod.cmd_merge(ns)
            except (OSError, sqlite3.Error, ValueError):
                pass


def _run_one(task_id: str, project_src: str | None) -> str:
    from hub.pipeline import cycle
    from hub.pipeline.runners import make_runner

    store = Store()
    task = store.get_task(task_id)
    if task is None:
        return "failed"
    try:
        from hub.config import load_project as _lp

        hint = project_src or str(task.get("worktree") or ".")
        project = _lp(hint)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return "failed"
    rounds, _after, blind = read_extra(task)
    exe_name = str(task.get("executor") or project.defaults.executor or "muse")
    try:
        rev_names = json.loads(task.get("reviewers_json") or "[]")
    except (TypeError, ValueError):
        rev_names = []
    if not isinstance(rev_names, list):
        rev_names = []
    try:
        executor = make_runner(exe_name)
    except ValueError:
        from hub.pipeline.runners import OpencodeRunner

        executor = OpencodeRunner()
    reviewers: dict = {}
    for name in rev_names:
        try:
            reviewers[str(name)] = make_runner(str(name))
        except ValueError:
            continue
    runners = {"executor": executor, "reviewers": reviewers}
    try:
        return cycle.run_task(store, project, task_id, runners, rounds=rounds, blind=blind)
    except (OSError, sqlite3.Error, ValueError) as e:
        try:
            store.upsert_task(id=task_id, stage="failed", stage_reason=f"queue-fail: {e}"[:500])
        except (OSError, sqlite3.Error, ValueError):
            pass
        return "failed"


def cmd_queue_run(args) -> int:
    proj_src = getattr(args, "project", None)
    try:
        project = load_project(proj_src) if proj_src else None
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"нет проекта: {e}")
        return 1
    store = Store()
    _owner_commands(store)
    if is_paused(store):
        print("пауза: meta.queue_paused")
        return 0
    try:
        max_par = max(1, int(getattr(args, "max_parallel", 1) or 1))
    except (TypeError, ValueError):
        max_par = 1
    try:
        queued = [t for t in store.list_tasks(active_only=False)
                  if str(t.get("stage") or "") == "queued"]
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    queued.sort(key=lambda t: int(t.get("created_at") or 0))
    # --after и пауза уже в _eligible; playerok — серийно.
    todo: list[dict] = []
    for t in queued:
        proj = project
        if proj is None:
            try:
                hint = str(t.get("worktree") or ".")
                proj = load_project(hint)
            except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
                print(f"SKIP {t['id']}: нет проекта")
                continue
        ok, why = _eligible(store, t, proj)
        if not ok:
            print(f"SKIP {t['id']}: {why}")
            continue
        todo.append(t)
    if not todo:
        print("(пусто)")
        return 0
    # playerok-задачи — строго по одной, не параллельно с такой же.
    normals: list[dict] = []
    playeroks: list[dict] = []
    for t in todo:
        try:
            proj = project or load_project(str(t.get("worktree") or "."))
            txt = card_text_of(t, proj) or ""
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
            txt = ""
        (playeroks if card_network_is_playerok(txt) else normals).append(t)
    code = 0
    if normals:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(max_par, len(normals))) as pool:
            futs = {pool.submit(_run_one, t["id"], proj_src): t["id"] for t in normals}
            for f in concurrent.futures.as_completed(futs):
                tid = futs[f]
                try:
                    print(f"DONE {tid}: {f.result()}")
                except (OSError, ValueError) as e:
                    print(f"FAIL {tid}: {e}")
                    code = 1
    for t in playeroks:
        try:
            print(f"DONE {t['id']}: {_run_one(t['id'], proj_src)}")
        except (OSError, ValueError) as e:
            print(f"FAIL {t['id']}: {e}")
            code = 1
    return code


def register(subparsers) -> None:
    p = subparsers.add_parser("queue", help="очередь задач")
    sub = p.add_subparsers(dest="queue_cmd", required=True)
    r = sub.add_parser("run", help="прогнать queued (фон: nohup hub queue run &)")
    r.add_argument("--max-parallel", type=int, default=4)
    r.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    r.set_defaults(func=cmd_queue_run)
