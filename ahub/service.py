"""Хаб-сервис: очередь, места, ресурсы, запуск процессов задач (architecture §2, §6, §13; contracts §8).

- Занятость — по живым процессам задач в /proc (`python -m ahub.worker T12`), а не по «своим детям»: после
  перезапуска сервиса задачи, начатые прежним экземпляром, продолжают работать и занимают места (урок v1 #3).
- Процесс задачи запускается в своей группе и не зависит от сервиса: перезапуск сервиса его не убивает.
- Задача из очереди запускается, когда: очередь не на паузе, все «после X» приняты, есть место в проекте
  (max_parallel), свободны её ресурсы (capacity + внешний flock не занят). Иначе — причина ожидания в задаче.
- Сервис состояние задач не меняет (кроме причины ожидания в очереди): задачу берёт её процесс (аренда).
- Сироты (V20): активная задача без живого процесса и с истёкшей арендой → обратно в очередь с событием и
  продолжение с места (та же сессия); повторное сиротство → «Нужно решение»; прерванное принятие → «Нужно решение».
"""

from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ahub import config, paths, procs
from ahub import log as hublog
from ahub import transitions
from ahub.model import ACTIVE, Ev, State
from ahub.store import Store, Task
from ahub.time import now_ms
from ahub.worker import CMD_MARK

SPAWN_GRACE_S = 30.0
ORPHAN_GRACE_MS = 60_000  # после истечения аренды — ещё минута на «вдруг процесс просто медленный»
MAX_ORPHANS = 1  # автоматический подхват — один раз  # после запуска процесс ещё может не быть виден / не взять задачу — не запускать повторно
HEARTBEAT_KEY = "service_heartbeat"
CODE_CHECK_S = 10.0  # как часто сверять код (самообновление)
PAUSE_KEY = "queue_paused"
_TASK_ARG = re.compile(r"^[Tt]?(\d+)$")


def live_workers(proc_root: str | Path = "/proc") -> dict[int, int]:
    """task_id → pid живых процессов задач на машине."""
    out: dict[int, int] = {}
    root = Path(proc_root)
    try:
        entries = [e for e in root.iterdir() if e.name.isdigit()]
    except OSError:
        return out
    for e in entries:
        args = procs.cmdline(int(e.name), proc_root)
        if not args or not any(CMD_MARK in a for a in args):
            continue
        try:
            i = next(i for i, a in enumerate(args) if CMD_MARK in a)
        except StopIteration:
            continue
        for a in args[i + 1:]:
            m = _TASK_ARG.match(a)
            if m:
                if procs.alive(int(e.name), proc_root):
                    out[int(m.group(1))] = int(e.name)
                break
    return out


def external_lock_busy(path: str) -> bool:
    """Внешний flock занят кем-то (тесты другого инструмента)? Не создаёт лишнего: файл открывается на чтение."""
    if not path:
        return False
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def spawn_worker(task_id: int) -> int:
    """Запустить процесс задачи отдельно от сервиса. Вывод — в state_dir/workers/T<id>.log."""
    d = paths.state_dir() / "workers"
    d.mkdir(parents=True, exist_ok=True)
    out = open(d / f"T{task_id}.log", "ab")
    try:
        p = subprocess.Popen([sys.executable, "-m", "ahub.worker", f"T{task_id}"], stdout=out, stderr=out,
                             stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(paths.data_dir()),
                             env=dict(os.environ))
    finally:
        out.close()
    return p.pid


@dataclass
class ProjectLoad:
    running: list[int] = field(default_factory=list)
    queued: int = 0
    waiting: dict[int, str] = field(default_factory=dict)


@dataclass
class TickReport:
    live: dict[int, int]
    spawned: list[int]
    load: dict[str, ProjectLoad]
    paused: bool


class Service:
    def __init__(self, store: Store, projects: list[config.ProjectConfig] | None = None, *,
                 spawn: Callable[[int], int] = spawn_worker, proc_root: str | Path = "/proc",
                 lock_busy: Callable[[str], bool] = external_lock_busy) -> None:
        self.store = store
        self._projects = projects
        self.spawn = spawn
        self.proc_root = proc_root
        self.lock_busy = lock_busy
        self.recent: dict[int, float] = {}  # task_id → когда запускали
        self.log = hublog.get("service")
        self._stop = threading.Event()

    def projects(self) -> list[config.ProjectConfig]:
        if self._projects is not None:
            return self._projects
        projects, errors = config.load_projects()
        for e in errors:
            self.log.warning("конфиг проекта: %s", e)
        return projects

    def paused(self) -> bool:
        return self.store.meta_get(PAUSE_KEY) == "1"

    def _deps_ok(self, t: Task) -> str:
        for a in t.after:
            dep = self.store.get_task(a)
            if dep is None:
                return f"нет задачи T{a}"
            if dep.state is not State.ACCEPTED:
                return f"ждёт принятия T{a} ({dep.state.value})"
        return ""

    def _set_wait(self, t: Task, reason: str) -> None:
        if t.state_reason != reason:
            self.store.update_task(t.id, state_reason=reason, phase="waiting" if reason else "")

    def tick(self) -> TickReport:
        live = live_workers(self.proc_root)
        now = time.monotonic()
        self.recent = {k: v for k, v in self.recent.items() if now - v < SPAWN_GRACE_S and k not in live}
        busy = set(live) | set(self.recent)
        paused = self.paused()
        spawned: list[int] = []
        load: dict[str, ProjectLoad] = {}
        self._orphans(live, busy)
        all_tasks = self.store.list_tasks(states=ACTIVE | {State.QUEUED})
        by_project: dict[str, list[Task]] = {}
        for t in all_tasks:
            by_project.setdefault(t.project, []).append(t)
        for project in self.projects():
            tasks = by_project.get(project.name, [])
            pl = load.setdefault(project.name, ProjectLoad())
            running = [t for t in tasks if t.id in busy]
            pl.running = [t.id for t in running]
            res_use: dict[str, int] = {}
            for t in running:
                for r in t.limits.get("resources") or []:
                    res_use[r] = res_use.get(r, 0) + 1
            queued = [t for t in tasks if t.state is State.QUEUED and t.id not in busy]
            pl.queued = len(queued)
            slots = project.max_parallel - len(running)
            for t in queued:
                reason = ""
                if paused:
                    reason = "очередь на паузе"
                if not reason:
                    reason = self._deps_ok(t)
                if not reason and slots <= 0:
                    reason = f"ждёт места ({len(running)}/{project.max_parallel})"
                if not reason:
                    for r in t.limits.get("resources") or []:
                        spec = project.resources.get(r)
                        if spec is None:
                            reason = f"ресурс {r} не объявлен проектом"
                            break
                        if res_use.get(r, 0) >= spec.capacity:
                            reason = f"ждёт ресурс {r}"
                            break
                        if spec.lock and self.lock_busy(spec.lock):
                            reason = f"ждёт ресурс {r} (занят вне хаба)"
                            break
                if reason:
                    pl.waiting[t.id] = reason
                    self._set_wait(t, reason)
                    continue
                self._set_wait(t, "")
                try:
                    pid = self.spawn(t.id)
                except OSError as e:
                    self.log.error("не запустился процесс T%d: %s", t.id, e, extra={"task": t.id})
                    pl.waiting[t.id] = f"не запустился процесс: {e}"
                    continue
                self.recent[t.id] = time.monotonic()
                spawned.append(t.id)
                running.append(t)
                slots -= 1
                for r in t.limits.get("resources") or []:
                    res_use[r] = res_use.get(r, 0) + 1
                self.log.info("запущен процесс T%d (pid %s)", t.id, pid, extra={"task": t.id})
        self.store.meta_set(HEARTBEAT_KEY, str(now_ms()))
        return TickReport(live=live, spawned=spawned, load=load, paused=paused)

    def _orphans(self, live: dict[int, int], busy: set[int]) -> list[int]:
        """Активные задачи без процесса: в очередь (продолжение с места) или «Нужно решение»."""
        now = now_ms()
        handled = []
        for t in self.store.list_tasks(states=ACTIVE):
            if t.id in busy or t.id in live:
                continue
            if t.owner and t.lease_until and t.lease_until + ORPHAN_GRACE_MS > now:
                continue  # аренда (или её грейс) ещё жива — владелец мог быть вне процесса задачи (CLI)
            if not t.owner and now - t.updated_at < ORPHAN_GRACE_MS:
                continue  # только что перешла — процесс ещё может появиться
            token = f"service:{os.getpid()}"
            if not transitions.acquire(self.store, t.id, token, pid=os.getpid(), lease_ms=30_000):
                continue
            count = int(t.limits.get("orphans") or 0) + 1
            lim = dict(t.limits)
            lim["orphans"] = count
            self.store.update_task(t.id, limits=lim)
            try:
                if t.state is State.ACCEPTING:
                    to, reason = State.NEEDS_DECISION, "принятие прервано (процесс исчез) — проверьте корень проекта"
                elif count > MAX_ORPHANS:
                    to, reason = State.NEEDS_DECISION, f"процесс задачи исчез повторно ({count} раз)"
                else:
                    to, reason = State.QUEUED, "процесс задачи исчез — продолжение с места"
                self.store.add_event(Ev.ORPHAN, task_id=t.id, project=t.project,
                                     payload={"from": t.state.value, "to": to.value, "count": count,
                                              "text": f"{t.label}: {reason}"})
                transitions.move(self.store, t.id, to, reason=reason, by="service", owner=token)
                self.log.warning("сирота T%d (%s) → %s", t.id, t.state.value, to.value, extra={"task": t.id})
                handled.append(t.id)
            finally:
                transitions.release(self.store, t.id, token)
        return handled

    def stop(self) -> None:
        self._stop.set()

    def _observe(self) -> None:
        """Наблюдатель — в своём потоке: разбор моделью не должен тормозить очередь."""
        from ahub import observer

        if self._obs is not None and self._obs.is_alive():
            return
        last = int(self.store.meta_get(observer.LAST_QUICK) or 0)
        if now_ms() - last < observer.QUICK_MS:
            return

        def _run():
            try:
                observer.cycle(self.store, projects=self.projects())
            except Exception:
                self.log.exception("наблюдатель упал")

        self._obs = threading.Thread(target=_run, name="observer", daemon=True)
        self._obs.start()

    def run_forever(self, poll_s: float = 2.0, *, self_update: bool = True, observe: bool = True) -> None:
        from ahub import observer

        self.log.info("сервис запущен (pid %d)", os.getpid())
        code0 = code_fingerprint()
        last_check = time.monotonic()
        self._obs = None

        def _sig(signum, frame):
            self.log.info("сигнал %d — останавливаюсь (процессы задач продолжают работу)", signum)
            self._stop.set()

        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(s, _sig)
            except ValueError:  # не главный поток (тесты)
                pass
        while not self._stop.is_set():
            try:
                self.tick()
                if observe:
                    self._observe()
                    observer.watchdog(self.store)
            except Exception:
                self.log.exception("тик сервиса упал")
            if self_update and time.monotonic() - last_check >= CODE_CHECK_S:
                last_check = time.monotonic()
                code = code_fingerprint()
                if code != code0:
                    ok, why = new_code_healthy()
                    if ok:
                        self.log.info("код хаба изменился — перезапуск сервиса на новый код (задачи не трогаются)")
                        restart_self()
                    else:
                        self.log.error("код хаба изменился, но не проходит проверку — остаюсь на старом: %s", why)
                        code0 = code  # не долбить проверкой каждые 10 с; следующее изменение проверим снова
            self._stop.wait(poll_s)
        self.log.info("сервис остановлен")


def code_fingerprint() -> str:
    """Отпечаток кода пакета ahub (время и размер .py-файлов): изменился — пора перезапуститься."""
    import hashlib

    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(root.rglob("*.py")):
        try:
            st = f.stat()
        except OSError:
            continue
        h.update(f"{f.relative_to(root)}:{st.st_mtime_ns}:{st.st_size};".encode())
    return h.hexdigest()


def new_code_healthy() -> tuple[bool, str]:
    """Новый код импортируется и отвечает — иначе не переключаемся (без цикла падений)."""
    try:
        r = subprocess.run([sys.executable, "-c", "import ahub.service, ahub.engine, ahub.worker, ahub.cli;"
                            "from ahub.store import Store; Store()"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip()[-300:]
    return True, ""


def restart_self() -> None:
    """Заменить процесс сервиса новым с тем же командным вызовом (pid сохраняется — systemd не заметит)."""
    os.execv(sys.executable, [sys.executable, "-m", "ahub", *sys.argv[1:]] if sys.argv[0].endswith("ahub")
             else [sys.executable, *sys.argv])
