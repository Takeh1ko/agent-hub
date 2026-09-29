"""hub queue: воркер очереди (queued → конвейер)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

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


def _project_worktrees_root(project) -> str:
    """Корень worktrees проекта (строкой, без слэша в конце)."""
    try:
        root = str(getattr(project, "worktrees", "") or "").strip()
    except (AttributeError, ValueError):
        return ""
    return root.rstrip("/")


def _belongs_to_project(task: dict, project) -> bool:
    """Задача — своего проекта: project == имя или пустой project + worktree внутри worktrees."""
    if project is None:
        return True
    try:
        want = str(getattr(project, "name", "") or "")
    except (AttributeError, ValueError):
        return False
    tp = str(task.get("project") or "").strip()
    if tp:
        return tp == want
    wt = str(task.get("worktree") or "").strip()
    if not wt:
        return False
    root = _project_worktrees_root(project)
    if not root:
        return False
    try:
        if not wt.startswith("/"):
            wt = str(Path.cwd() / wt)
        return wt == root or wt.startswith(root + "/")
    except (OSError, ValueError):
        return False


def _is_work_stage(stage: str) -> bool:
    """Этапы, которые ведёт конвейер (не трогаем при перезапуске с живым процессом)."""
    s = str(stage or "")
    return s == "preflight" or s.startswith("exec r") or s.startswith("gate r") \
        or s.startswith("review r")


def _lock_key_of(task: dict, project) -> str:
    """Замок тестов проекта НЕ сериализует задачи: его ждут только прогоны приёмки в воротах
    (check_gate ждёт замок), а писать код агенты могут параллельно (2026-09-29: сериализация
    гнала все задачи PlayerUP гуськом при 3 свободных местах)."""
    return ""


def _is_playerok(task: dict, project) -> bool:
    """Задача с «Сеть: playerok» — только одна одновременно."""
    try:
        txt = card_text_of(task, project) or ""
    except (OSError, ValueError):
        txt = ""
    try:
        return bool(card_network_is_playerok(txt))
    except (ValueError, AttributeError):
        return False


def _live_run_one(proc_root: str | Path = "/proc") -> dict[str, int]:
    """Живые дочерние `--run-one`: task_id → pid (прямое чтение cmdline)."""
    out: dict[str, int] = {}
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return {}
    for e in entries:
        if not e.name.isdigit():
            continue
        try:
            raw = (e / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        args = [a for a in raw.split("\x00") if a]
        if "--run-one" not in args:
            continue
        try:
            idx = args.index("--run-one")
        except ValueError:
            continue
        if idx + 1 >= len(args):
            continue
        tid = str(args[idx + 1]).strip()
        if not tid or tid.startswith("-"):
            continue
        try:
            out[tid] = int(e.name)
        except (TypeError, ValueError):
            continue
    return out


def _run_one_cmd(task_id: str, proj_src: str | None) -> list[str]:
    """Команда дочернего процесса: свежий код hub на каждую задачу."""
    cmd = [sys.executable, "-m", "hub.commands.queue", "--run-one", str(task_id)]
    if proj_src:
        cmd += ["--project", str(proj_src)]
    return cmd


def _spawn_one(task_id: str, proj_src: str | None):
    """Запустить задачу в отдельном процессе (своя группа — переживает рестарт воркера)."""
    cmd = _run_one_cmd(str(task_id), proj_src)
    env = dict(os.environ)
    env["HUB_QUEUE_CHILD"] = "1"
    return subprocess.Popen(cmd, start_new_session=True, env=env)


class _ThreadSlot:
    """Слот --once: _run_one в потоке (синхронный проход, моки раннеров видны)."""

    def __init__(self, task_id: str, proj_src: str | None) -> None:
        self.tid = str(task_id)
        self.pid: int | None = None
        self.returncode: int | None = None
        self.result: str = ""
        self._done = threading.Event()
        th = threading.Thread(target=self._boot, args=(str(task_id), proj_src),
                              daemon=True)
        th.start()
        try:
            self.pid = th.ident
        except (AttributeError, ValueError):
            self.pid = None

    def _boot(self, task_id: str, proj_src: str | None) -> None:
        try:
            self.result = _run_one(task_id, proj_src)
            self.returncode = 0
        except Exception:
            # Любой сюрприз из _run_one — слот обязан завершиться, иначе
            # _pump ждёт poll() вечно (у процессов тот же контракт даёт код выхода).
            self.returncode = 1
        finally:
            self._done.set()

    def poll(self):
        if not self._done.is_set():
            return None
        # Страховка: done без кода (сторонний spawn_fn) — провал, не вечное ожидание.
        return self.returncode if self.returncode is not None else 1


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


def _mark_taken(store: Store, task_id: str) -> None:
    """Честный этап взятой очередью задачи: queued → exec rN до исполнителя (H13 п.4).

    N — текущий круг (round+1, минимум 1). Только из queued, чужие этапы
    не трогаем. Best effort: ошибки store глотаются.
    """
    try:
        task = store.get_task(task_id)
    except (OSError, sqlite3.Error, ValueError):
        return
    if task is None:
        return
    try:
        if str(task.get("stage") or "") != "queued":
            return
    except (AttributeError, TypeError):
        return
    try:
        cur = int(task.get("round") or 0)
    except (TypeError, ValueError):
        cur = 0
    n = cur + 1 if cur and cur > 0 else 1
    if n < 1:
        n = 1
    stage = f"exec r{n}"
    try:
        store.upsert_task(id=task_id, stage=stage, round=n,
                          stage_reason="очередь взяла задачу")
    except (OSError, sqlite3.Error, ValueError):
        return
    try:
        store.add_event(task_id, "stage", {"stage": stage, "round": n,
                                           "why": "queue-taken"})
    except (OSError, sqlite3.Error, ValueError):
        pass


def _run_one(task_id: str, project_src: str | None) -> str:
    from hub.pipeline import cycle
    from hub.pipeline.runners import make_runner

    store = Store()
    task = store.get_task(task_id)
    if task is None:
        return "failed"
    # Этап сразу честный, до запуска исполнителя (гонка старта).
    _mark_taken(store, task_id)
    try:
        task = store.get_task(task_id) or task
    except (OSError, sqlite3.Error, ValueError):
        pass
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


def _list_queued_all(store: Store) -> list[dict]:
    """Все queued по created_at (фолбэк, если нет store.list_queued)."""
    try:
        fn = getattr(store, "list_queued", None)
        if callable(fn):
            return list(fn())
    except (OSError, sqlite3.Error, ValueError):
        pass
    try:
        return [t for t in store.list_tasks(active_only=False)
                if str(t.get("stage") or "") == "queued"]
    except (OSError, sqlite3.Error, ValueError):
        return []


def _pump(store: Store, project, proj_src: str | None, max_par: int,
          spawn_fn=None, poll_secs: float = 0.05, proc_root: str | Path = "/proc",
          stop_fn=None) -> int:
    """Проход очереди со слотами: свободное место + eligible queued — сразу взять.

    Слоты разбираются по мере освобождения (не ждём всю пачку), снимок queued
    перечитывается каждую итерацию. Playerok — строго одна одновременно; общий
    test_lock — тоже по одному. Чужие проекты (при --project) не трогаем никогда.
    Задачи в работе с живым `--run-one` при перезапуске не трогаем (только queued).
    По умолчанию слот — поток с _run_one в этом процессе (синхронный --once,
    моки раннеров видны); фон передает spawn_fn с отдельным процессом.
    stop_fn (SIGTERM/SIGINT/queue_stop) проверяется каждую итерацию: новых не берём,
    идущие продолжают в фоне — рестарт быстрый и никого не убивает.
    """
    _owner_commands(store)
    if is_paused(store):
        print("пауза: meta.queue_paused")
        return 0
    try:
        max_par = max(1, int(max_par or 1))
    except (TypeError, ValueError):
        max_par = 1
    try:
        poll_secs = max(0.01, float(poll_secs or 0.05))
    except (TypeError, ValueError):
        poll_secs = 0.05
    if spawn_fn is None:
        def _default_spawn(tid: str):
            return _ThreadSlot(tid, proj_src)
        spawn_fn = _default_spawn

    def _stopped() -> bool:
        if stop_fn is None:
            return False
        try:
            return bool(stop_fn())
        except (OSError, ValueError):
            return False

    running: dict[str, object] = {}
    info: dict[str, tuple[bool, str]] = {}  # tid -> (playerok, lock_key)
    code = 0
    first_pass = True
    while True:
        # Завершившиеся — снять со слотов.
        for tid in list(running):
            proc = running[tid]
            try:
                rc = proc.poll()  # type: ignore[attr-defined]
            except (OSError, ValueError, AttributeError) as e:
                # Состояние процесса неизвестно — честный FAIL, не ложный DONE.
                print(f"FAIL {tid}: poll-error: {e}")
                code = 1
                running.pop(tid, None)
                info.pop(tid, None)
                _owner_commands(store)
                continue
            if rc is None:
                continue
            try:
                fresh = store.get_task(tid)
                stage = str((fresh or {}).get("stage") or "?")
            except (OSError, sqlite3.Error, ValueError):
                stage = "?"
            try:
                pid = getattr(proc, "pid", "?")
            except (AttributeError, ValueError):
                pid = "?"
            if rc != 0:
                print(f"FAIL {tid}: rc={rc} stage={stage} pid={pid}")
                code = 1
            else:
                print(f"DONE {tid}: {stage} pid={pid}")
            running.pop(tid, None)
            info.pop(tid, None)
            _owner_commands(store)
        if _stopped():
            # Быстрый выход: новых не берём, идущие продолжают в фоне
            # (своя группа у процессов, daemon-потоки у --once).
            if running:
                print(f"стоп: {len(running)} продолжают в фоне")
            return code
        if is_paused(store):
            if not running:
                print("пауза: meta.queue_paused")
                return code
            time.sleep(poll_secs)
            continue
        # Снимок queued на каждую итерацию — новые задачи подбираем сразу.
        try:
            if project is not None:
                try:
                    fn = getattr(store, "list_queued_for_project", None)
                    if callable(fn):
                        queued = list(fn(project.name,
                                         getattr(project, "worktrees", "") or ""))
                    else:
                        queued = [t for t in _list_queued_all(store)
                                  if _belongs_to_project(t, project)]
                except (OSError, sqlite3.Error, ValueError) as e:
                    print(f"store-fail: {e}")
                    return 1
            else:
                queued = _list_queued_all(store)
        except (OSError, sqlite3.Error, ValueError) as e:
            print(f"store-fail: {e}")
            return 1
        try:
            queued.sort(key=lambda t: int(t.get("created_at") or 0))
        except (TypeError, ValueError):
            pass
        try:
            live = _live_run_one(proc_root)
        except (OSError, ValueError):
            live = {}
        playerok_busy = any(v[0] for v in info.values())
        locks_busy = {v[1] for v in info.values() if v[1]}
        spawnable: list[tuple[dict, bool, str]] = []
        skips: list[str] = []
        for t in queued:
            if len(running) + len(spawnable) >= max_par:
                break
            tid = str(t.get("id") or "")
            if not tid or tid in running or tid in live:
                continue
            if project is not None and not _belongs_to_project(t, project):
                continue
            proj_for = project
            if proj_for is None:
                try:
                    proj_for = _resolve_project(t, None)
                except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
                    print(f"NO-PROJECT {tid}: .hub.toml не найден "
                          f"(ни worktree, ни ~/.config/agent-hub/config.toml)")
                    try:
                        store.upsert_task(id=tid, stage="failed",
                                          stage_reason="no-project: .hub.toml не найден")
                        store.add_event(tid, "stage",
                                        {"stage": "failed", "reason": "no-project"})
                    except (OSError, sqlite3.Error, ValueError):
                        pass
                    code = 1
                    continue
            ok, why = _eligible(store, t, proj_for)
            if not ok:
                if first_pass:
                    skips.append(f"SKIP {tid}: {why}")
                continue
            is_pok = _is_playerok(t, proj_for)
            lock_key = _lock_key_of(t, proj_for)
            if is_pok and playerok_busy:
                continue
            if lock_key and lock_key in locks_busy:
                continue
            spawnable.append((t, is_pok, lock_key))
            if is_pok:
                playerok_busy = True
            if lock_key:
                locks_busy.add(lock_key)
        if first_pass:
            for line in skips:
                print(line)
            first_pass = False
        if spawnable:
            for t, is_pok, lock_key in spawnable:
                tid = str(t["id"])
                try:
                    proc = spawn_fn(tid)
                except (OSError, ValueError, RuntimeError) as e:
                    print(f"FAIL {tid}: {e}")
                    code = 1
                    continue
                running[tid] = proc
                info[tid] = (is_pok, lock_key)
                # Честный этап сразу, не дожидаясь старта дочернего процесса.
                try:
                    _mark_taken(store, tid)
                except (OSError, ValueError):
                    pass
                try:
                    pid = getattr(proc, "pid", "?")
                except (AttributeError, ValueError):
                    pid = "?"
                print(f"START {tid} pid={pid}")
            continue
        if not running:
            if not queued:
                print("(пусто)")
            return code
        time.sleep(poll_secs)


def _queue_stop(store: Store) -> bool:
    """Мягкий стоп цикла (для тестов и ручной остановки без сигналов)."""
    try:
        return (meta_get(store, "queue_stop") or "") == "1"
    except (OSError, ValueError):
        return False


def cmd_queue_run(args) -> int:
    # Дочерний процесс одной задачи: свежий код, свой результат в store.
    run_one = getattr(args, "run_one", None) or getattr(args, "run_one_id", None)
    if run_one:
        tid = str(run_one)
        proj_src = getattr(args, "project", None)
        try:
            res = _run_one(tid, proj_src)
        except (OSError, sqlite3.Error, ValueError) as e:
            print(f"FAIL {tid}: {e}")
            return 1
        if os.environ.get("HUB_QUEUE_CHILD") != "1":
            print(f"DONE {tid}: {res}")
        return 0
    proj_src = getattr(args, "project", None)
    project = None
    if proj_src:
        try:
            project = load_project(proj_src)
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
            print(f"нет проекта: {e}")
            return 1
    max_par = _max_par_of(args)
    try:
        poll = max(0.01, float(getattr(args, "poll_secs", 10) or 10))
    except (TypeError, ValueError):
        poll = 10.0
    store = Store()
    if bool(getattr(args, "once", False)):
        # Синхронный проход в этом процессе (потоки): --once для тестов/скриптов,
        # моки раннеров видны, результат печатается сразу. Устаревания кода нет —
        # процесс выходит после прохода.
        return _pump(store, project, proj_src, max_par, poll_secs=poll)
    # Долгоживущий фон: каждая задача — отдельный процесс --run-one в своей
    # группе (свежий код hub на задачу, рестарт воркера идущие не убивает);
    # перезапуск не трогает exec/gate/review с живым --run-one
    # (берём только queued, см. _pump). Выход — SIGTERM/SIGINT или queue_stop=1.
    def _proc_spawn(tid: str):
        return _spawn_one(tid, proj_src)
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
    def _stopped() -> bool:
        return stop_ev.is_set() or _queue_stop(store)

    code = 0
    while True:
        if _stopped():
            break
        code = _pump(store, project, proj_src, max_par,
                     spawn_fn=_proc_spawn, poll_secs=poll, stop_fn=_stopped)
        if _stopped():
            break
        # time.sleep — воркер не держит store дольше транзакции.
        if stop_ev.wait(poll):
            break
    print("стоп: очередь остановлена")
    return code


def cmd_queue_status(args) -> int:
    """Кто в работе (pid, задача, этап), сколько в очереди, свободных мест."""
    proj_src = getattr(args, "project", None)
    project = None
    if proj_src:
        try:
            project = load_project(proj_src)
        except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
            print(f"нет проекта: {e}")
            return 1
    max_par = _max_par_of(args)
    as_json = bool(getattr(args, "json", False))
    store = Store()
    try:
        live = _live_run_one("/proc")
    except (OSError, ValueError):
        live = {}
    # В работе: живые --run-one плюс орфаны — задачи в exec/gate/review/preflight
    # без живого процесса (убит -9, воркер упал). Орфаны занимают места тоже,
    # иначе свободные места завышаются.
    try:
        fn = getattr(store, "list_running_tasks", None)
        if callable(fn):
            work = list(fn())
        else:
            work = [t for t in store.list_tasks(active_only=False)
                    if _is_work_stage(str(t.get("stage") or ""))]
    except (OSError, sqlite3.Error, ValueError):
        work = []
    work = [t for t in work if _is_work_stage(str(t.get("stage") or ""))]
    if project is not None:
        work = [t for t in work if _belongs_to_project(t, project)]
    live_ids = set(live)
    running: list[dict] = []
    for t in work:
        tid = str(t.get("id") or "")
        if not tid:
            continue
        stage = str(t.get("stage") or "")
        if tid in live_ids:
            running.append({"pid": live[tid], "task": tid, "stage": stage,
                            "orphan": False})
        else:
            running.append({"pid": None, "task": tid, "stage": stage,
                            "orphan": True})
    # Живые --run-one, чья стадия ещё не ушла из queued (гонка старта),
    # тоже в работе — не в очереди.
    for tid, pid in sorted(live.items(), key=lambda kv: kv[1]):
        if any(r["task"] == tid for r in running):
            continue
        try:
            t = store.get_task(tid)
        except (OSError, sqlite3.Error, ValueError):
            t = None
        if t is None:
            continue
        if project is not None and not _belongs_to_project(t, project):
            # Чужой проект в статусе своего не показываем в работе.
            continue
        running.append({"pid": pid, "task": tid,
                        "stage": str(t.get("stage") or ""), "orphan": False})
    try:
        if project is not None:
            fn = getattr(store, "list_queued_for_project", None)
            if callable(fn):
                queued = list(fn(project.name,
                                 getattr(project, "worktrees", "") or ""))
            else:
                queued = [t for t in _list_queued_all(store)
                          if _belongs_to_project(t, project)]
            # Перепроверка воркером, как в _pump (store — только предвыборка).
            queued = [t for t in queued if _belongs_to_project(t, project)]
        else:
            queued = _list_queued_all(store)
    except (OSError, sqlite3.Error, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    # Живые --run-one уже не queued (стадия ушла), но на случай гонки —
    # из очереди их вычитаем.
    queued_ids = [str(t.get("id") or "") for t in queued
                  if str(t.get("id") or "") not in live_ids]
    free = max(0, max_par - len(running))
    if as_json:
        print(json.dumps({
            "running": running,
            "queued": len(queued_ids),
            "queued_ids": sorted(queued_ids),
            "max_parallel": max_par,
            "free": free,
        }, ensure_ascii=False))
        return 0
    if running:
        for r in sorted(running, key=lambda x: int(x["pid"] or 0) if x["pid"] else 0):
            pid = r["pid"] if r["pid"] is not None else "—"
            tail = " (орфан)" if r.get("orphan") else ""
            print(f"RUN {pid} {r['task']} {r['stage']}{tail}")
    else:
        print("в работе: —")
    print(f"очередь: {len(queued_ids)}")
    print(f"в работе: {len(running)}/{max_par}, свободно: {free}")
    return 0


def _child_main(argv: list[str] | None = None) -> int:
    """Точка `python -m hub.commands.queue --run-one ID [--project P]`."""
    import argparse as _ap

    ap = _ap.ArgumentParser(prog="hub.commands.queue")
    ap.add_argument("--run-one", default=None)
    ap.add_argument("--project", default=None)
    ns = ap.parse_args(argv)
    if not ns.run_one:
        ap.print_help()
        return 2
    try:
        res = _run_one(str(ns.run_one), ns.project)
    except (OSError, sqlite3.Error, ValueError) as e:
        print(f"FAIL {ns.run_one}: {e}")
        return 1
    if os.environ.get("HUB_QUEUE_CHILD") != "1":
        print(f"DONE {ns.run_one}: {res}")
    return 0


main = _child_main


def register(subparsers) -> None:
    p = subparsers.add_parser("queue", help="очередь задач")
    sub = p.add_subparsers(dest="queue_cmd", required=True)
    r = sub.add_parser("run", help="прогнать queued (фон: nohup hub queue run &)")
    r.add_argument("--max-parallel", type=int, default=4)
    r.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    r.add_argument("--once", action="store_true",
                   help="один проход очереди (для тестов)")
    r.add_argument("--poll-secs", type=float, default=10,
                   help="пауза опроса очереди и owner_command в цикле, сек")
    r.add_argument("--run-one", default=None,
                   help="одна задача ID в этом процессе (дочерний слот очереди)")
    r.set_defaults(func=cmd_queue_run)
    s = sub.add_parser("status", help="кто в работе, очередь, свободные места")
    s.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    s.add_argument("--max-parallel", type=int, default=4)
    s.add_argument("--json", action="store_true", help="машинный JSON")
    s.set_defaults(func=cmd_queue_status)


if __name__ == "__main__":
    raise SystemExit(_child_main())
