"""hub queue: воркер очереди (queued → конвейер)."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import threading
import tomllib

from hub.config import load_project, load_projects
from hub.pipeline.common import (
    card_text_of,
    card_network_is_playerok,
    is_paused,
    meta_get,
    meta_set,
    read_extra,
)
from hub.store import Store

OWNER_META = "last_owner_cmd_id"


def _resolve_project(task: dict, project_src: str | None):
    """Проект задачи: явный --project, иначе worktree, иначе глобальный список."""
    if project_src:
        return load_project(project_src)
    hint = str(task.get("worktree") or ".")
    try:
        return load_project(hint)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        pass
    want = str(task.get("project") or "")
    if want:
        for p in load_projects():
            if p.name == want:
                return p
    return load_project(hint)


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
    """События owner_command (H05 /stop /merge): каждое — ровно один раз."""
    try:
        last = int(meta_get(store, OWNER_META) or 0)
    except (TypeError, ValueError):
        last = 0
    try:
        rows = store.events_since(last)
    except (OSError, sqlite3.Error, ValueError):
        return
    done = last
    for e in rows:
        done = max(done, int(e.get("id") or 0))
        if str(e.get("kind") or "") != "owner_command":
            continue
        try:
            payload = json.loads(e.get("payload_json") or "{}")
        except (TypeError, ValueError):
            continue
        cmd = str(payload.get("cmd") or payload.get("action") or "")
        # Бот H05 кладёт id задачи в колонку event.task_id, а в payload —
        # только {"action": "stop"}; читаем оба места.
        tid = str(payload.get("task_id") or payload.get("id")
                  or e.get("task_id") or "")
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
    meta_set(store, OWNER_META, str(done))


def _partition(todo: list[dict], lock_of) -> tuple[list[dict], list[list[dict]]]:
    """Разложить queued: без замка — параллельно, с общим замком — серийно.

    Один test_lock на проект (§11) без ожидания даёт случайные failed
    (locked → failed), поэтому задачи с одинаковым замком идут по одной.
    """
    free: list[dict] = []
    groups: dict[str, list[dict]] = {}
    for t in todo:
        try:
            key = lock_of(t) or ""
        except (OSError, ValueError):
            key = ""
        if key:
            groups.setdefault(key, []).append(t)
        else:
            free.append(t)
    serial = [g for g in groups.values()]
    return free, serial


def _run_one(task_id: str, project_src: str | None) -> str:
    from hub.pipeline import cycle
    from hub.pipeline.runners import make_runner

    store = Store()
    task = store.get_task(task_id)
    if task is None:
        return "failed"
    try:
        project = _resolve_project(task, project_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        try:
            store.upsert_task(id=task_id, stage="failed",
                              stage_reason="no-project: .hub.toml не найден")
        except (OSError, sqlite3.Error, ValueError):
            pass
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
        # Ошибка конфига — громко, а не дефолтный раннер молча.
        try:
            store.upsert_task(id=task_id, stage="failed",
                              stage_reason=f"неизвестный executor: {exe_name}"[:500])
            store.add_event(task_id, "stage",
                            {"stage": "failed", "reason": f"неизвестный executor: {exe_name}"})
        except (OSError, sqlite3.Error, ValueError):
            pass
        return "failed"
    reviewers: dict = {}
    unknown: list[str] = []
    for name in rev_names:
        try:
            reviewers[str(name)] = make_runner(str(name))
        except ValueError:
            unknown.append(str(name))
    if unknown:
        try:
            store.add_event(task_id, "stage",
                            {"stage": str(task.get("stage") or ""),
                             "warning": f"неизвестные ревьюеры пропущены: {','.join(unknown)}"})
        except (OSError, sqlite3.Error, ValueError):
            pass
    if rev_names and not reviewers:
        try:
            store.upsert_task(id=task_id, stage="failed",
                              stage_reason=f"нет валидных ревьюеров: {','.join(unknown)}"[:500])
        except (OSError, sqlite3.Error, ValueError):
            pass
        return "failed"
    runners = {"executor": executor, "reviewers": reviewers}
    try:
        return cycle.run_task(store, project, task_id, runners, rounds=rounds, blind=blind)
    except (OSError, sqlite3.Error, ValueError) as e:
        try:
            store.upsert_task(id=task_id, stage="failed", stage_reason=f"queue-fail: {e}"[:500])
        except (OSError, sqlite3.Error, ValueError):
            pass
        return "failed"


def _max_par_of(args) -> int:
    try:
        return max(1, int(getattr(args, "max_parallel", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _pump(store: Store, project, proj_src: str | None, max_par: int) -> int:
    """Один проход очереди: owner-команды → снимок queued → запуск.

    Owner-команды перечитываются и после каждой завершённой задачи —
    /stop и /merge, пришедшие во время многочасового прогона, не ждут
    следующего запуска.
    """
    _owner_commands(store)
    if is_paused(store):
        print("пауза: meta.queue_paused")
        return 0
    try:
        queued = [t for t in store.list_tasks(active_only=False)
                  if str(t.get("stage") or "") == "queued"]
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    queued.sort(key=lambda t: int(t.get("created_at") or 0))
    todo: list[dict] = []
    code = 0
    for t in queued:
        proj = project
        if proj is None:
            try:
                proj = _resolve_project(t, None)
            except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
                print(f"NO-PROJECT {t['id']}: .hub.toml не найден "
                      f"(ни worktree, ни ~/.config/agent-hub/config.toml)")
                # Не висим в queued вечно с exit 1 на каждом прогоне.
                try:
                    store.upsert_task(id=t["id"], stage="failed",
                                      stage_reason="no-project: .hub.toml не найден")
                    store.add_event(t["id"], "stage",
                                    {"stage": "failed", "reason": "no-project"})
                except (OSError, sqlite3.Error, ValueError):
                    pass
                code = 1
                continue
        ok, why = _eligible(store, t, proj)
        if not ok:
            print(f"SKIP {t['id']}: {why}")
            continue
        todo.append(t)
    if not todo:
        print("(пусто)" if code == 0 else "(пусто, были ошибки проекта)")
        return code
    # playerok-задачи — строго по одной, не параллельно с такой же.
    normals: list[dict] = []
    playeroks: list[dict] = []
    for t in todo:
        try:
            proj = project or _resolve_project(t, None)
            txt = card_text_of(t, proj) or ""
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError, ValueError):
            txt = ""
        (playeroks if card_network_is_playerok(txt) else normals).append(t)

    def _lock_of(t: dict) -> str:
        try:
            proj = project or _resolve_project(t, None)
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError, ValueError):
            return ""
        return (getattr(proj, "test_lock", "") or "").strip()

    free, serial_groups = _partition(normals, _lock_of)
    if free:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(max_par, len(free))) as pool:
            futs = {pool.submit(_run_one, t["id"], proj_src): t["id"] for t in free}
            for f in concurrent.futures.as_completed(futs):
                tid = futs[f]
                try:
                    print(f"DONE {tid}: {f.result()}")
                except (OSError, ValueError) as e:
                    print(f"FAIL {tid}: {e}")
                    code = 1
                _owner_commands(store)
    for grp in serial_groups:
        for t in grp:
            try:
                print(f"DONE {t['id']}: {_run_one(t['id'], proj_src)}")
            except (OSError, ValueError) as e:
                print(f"FAIL {t['id']}: {e}")
                code = 1
            _owner_commands(store)
    for t in playeroks:
        try:
            print(f"DONE {t['id']}: {_run_one(t['id'], proj_src)}")
        except (OSError, ValueError) as e:
            print(f"FAIL {t['id']}: {e}")
            code = 1
        _owner_commands(store)
    return code


def _queue_stop(store: Store) -> bool:
    """Мягкий стоп цикла (для тестов и ручной остановки без сигналов)."""
    try:
        return (meta_get(store, "queue_stop") or "") == "1"
    except (OSError, ValueError):
        return False


def cmd_queue_run(args) -> int:
    proj_src = getattr(args, "project", None)
    project = None
    if proj_src:
        try:
            project = load_project(proj_src)
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
            print(f"нет проекта: {e}")
            return 1
    max_par = _max_par_of(args)
    store = Store()
    if bool(getattr(args, "once", False)):
        return _pump(store, project, proj_src, max_par)
    # Долгоживущий цикл: опрос очереди и owner_command; выход —
    # SIGTERM/SIGINT или meta queue_stop=1 (по умолчанию раз в 10 с).
    try:
        poll = max(0.05, float(getattr(args, "poll_secs", 10) or 10))
    except (TypeError, ValueError):
        poll = 10.0
    stop_ev = threading.Event()

    def _on_sig(signum, frame) -> None:
        stop_ev.set()

    try:
        import signal as _sig

        for _s in (_sig.SIGTERM, _sig.SIGINT):
            try:
                _sig.signal(_s, _on_sig)
            except (OSError, ValueError, RuntimeError):
                pass
    except ImportError:
        pass
    code = 0
    while True:
        if stop_ev.is_set() or _queue_stop(store):
            break
        code = _pump(store, project, proj_src, max_par)
        if stop_ev.is_set() or _queue_stop(store):
            break
        # time.sleep — воркер не держит store дольше транзакции.
        if stop_ev.wait(poll):
            break
    print("стоп: очередь остановлена")
    return code


def register(subparsers) -> None:
    p = subparsers.add_parser("queue", help="очередь задач")
    sub = p.add_subparsers(dest="queue_cmd", required=True)
    r = sub.add_parser("run", help="прогнать queued (фон: nohup hub queue run &)")
    r.add_argument("--max-parallel", type=int, default=4)
    r.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    r.add_argument("--once", action="store_true",
                   help="один проход очереди (для тестов)")
    r.add_argument("--poll-secs", type=float, default=10,
                   help="опрос очереди и owner_command в цикле, c")
    r.set_defaults(func=cmd_queue_run)
