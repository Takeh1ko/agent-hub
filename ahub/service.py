"""Hub service: queue, slots, resources, task process launches (architecture §2, §6, §13; contracts §8).

- Busyness counts live task processes in /proc (`python -m ahub.worker T12`), not "own children": after
  a service restart, tasks started by the previous instance keep running and hold slots (v1 lesson #3).
- A task process runs in its own group, independent of the service: a service restart does not kill it.
- A queued task launches when: the queue is not paused, all "after X" are accepted, the project has
  a free slot (max_parallel), and its resources are free (capacity + external flock not held). Otherwise —
  the wait reason is stored on the task. Resources are only those the task names: the project test resource is
  not added implicitly (acceptance takes its lock by itself, gates) — code tasks run in parallel, and their
  acceptance runs queue up on the lock.
- The service never changes task states (except the queue wait reason): the task's own process claims it (lease).
- Orphans (V20): an active task with no live process and an expired lease → back to queue with an event and
  resume in place (same session); repeated orphaning → "Needs decision"; interrupted acceptance → "Needs
  decision" with a reason that points at `ahub accept` (the merge may already be in the work branch — accept
  skips the gates and the merge then and finishes the tail). An "accepting" task whose owner process is alive is
  never an orphan (acceptance is long; accept.py renews the lease while it runs).
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

from ahub import config, paths, procs, quota, reasons, transitions
from ahub import log as hublog
from ahub.model import ACTIVE, Ev, Kind, State
from ahub.selfupdate import CODE_CHECK_S, code_fingerprint, hub_env, new_code_healthy, restart_self
from ahub.store import Store, Task
from ahub.time import now_ms
from ahub.worker import CMD_MARK

SPAWN_GRACE_S = 30.0  # after spawn the process may not be visible / may not have claimed the task yet
ORPHAN_GRACE_MS = 60_000  # past lease expiry — another minute in case the process is just slow
MAX_ORPHANS = 1  # one automatic pickup
HEARTBEAT_KEY = "service_heartbeat"
PAUSE_KEY = "queue_paused"
# The OS services, named once: `ahub service install` (commands/service.py) builds its maps from these and
# `ahub doctor` reads the environment of the hub unit through them — a name in two places would leave the
# doctor reading a file that no install writes.
UNIT_SERVICE = "ahub.service"  # the systemd unit of the hub service
UNIT_BOT = "ahub-bot.service"  # the bot's unit (installed only with a [telegram] token)
LABELS = {UNIT_SERVICE: "dev.ahub.service", UNIT_BOT: "dev.ahub.bot"}  # the launchd labels
PLIST_FILES = {UNIT_SERVICE: "dev.ahub.service.plist", UNIT_BOT: "dev.ahub.bot.plist"}
_TASK_ARG = re.compile(r"^[Tt]?(\d+)$")


def _worker_task(args: list[str]) -> int | None:
    """Task id of a `python -m ahub.worker T<n>` command line; None — anything else.

    The mark must be an argument of its own, right after the module flag: a shell command, a grep pattern
    or an agent prompt that merely mentions "ahub.worker T1" is not a task process — such a process once
    hid a dead task from the pulse and took a slot in the queue.
    """
    for i, a in enumerate(args):
        if a != CMD_MARK and not a.endswith(f"/{CMD_MARK}"):
            continue
        if i == 0 or args[i - 1] != "-m":  # the only form the service spawns: python -m ahub.worker T<n>
            continue
        for rest in args[i + 1:]:
            m = _TASK_ARG.match(rest)
            if m:
                return int(m.group(1))
    return None


def _accept_task(args: list[str]) -> int | None:
    """Task id of an `ahub accept T<n>` command line; None — anything else.

    As with `_worker_task`, the marks are arguments of their own: another process that merely mentions
    `accept` and a task number is not this task's acceptance, and its pid must not hide a dead task forever.
    """
    for i, a in enumerate(args):
        if a != "accept":
            continue
        for rest in args[i + 1:]:
            m = _TASK_ARG.match(rest)
            if m:
                return int(m.group(1))
    return None


def accepting_task(pid: int, task_id: int, proc_root: str | Path = "/proc") -> bool:
    """True if pid runs the acceptance of this task (acceptance is long — its lease may look stale)."""
    return _accept_task(procs.cmdline(pid, proc_root)) == task_id


def live_workers(proc_root: str | Path = "/proc") -> dict[int, int]:
    """task_id → pid of live task processes on this machine."""
    out: dict[int, int] = {}
    for pid in procs.pids(proc_root):
        tid = _worker_task(procs.cmdline(pid, proc_root))
        if tid is not None and procs.alive(pid, proc_root):
            out[tid] = pid
    return out


def external_lock_busy(path: str) -> bool:
    """Whether an external flock is held by someone (another tool's tests). Creates nothing extra: opens read-only."""
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
    """Start a task process detached from the service. Output — state_dir/workers/T<id>.log."""
    d = paths.state_dir() / "workers"
    d.mkdir(parents=True, exist_ok=True)
    out = open(d / f"T{task_id}.log", "ab")
    try:
        p = subprocess.Popen([sys.executable, "-m", "ahub.worker", f"T{task_id}"], stdout=out, stderr=out,
                             stdin=subprocess.DEVNULL, start_new_session=True, cwd=str(paths.data_dir()),
                             env=hub_env())
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
        self.recent: dict[int, float] = {}  # task_id → when it was spawned
        self.log = hublog.get("service")
        self._stop = threading.Event()

    def projects(self) -> list[config.ProjectConfig]:
        if self._projects is not None:
            return self._projects
        projects, errors = config.load_projects()
        for e in errors:
            self.log.warning("project config: %s", e)
        return projects

    def paused(self) -> bool:
        return self.store.meta_get(PAUSE_KEY) == "1"

    def _deps_ok(self, t: Task) -> str:
        """Why the task cannot start yet (a reason code blob), or "" — all its "after X" are accepted."""
        for a in t.after:
            dep = self.store.get_task(a)
            if dep is None:
                return reasons.dump("dep_missing", task=f"T{a}")
            if dep.state is not State.ACCEPTED:
                return reasons.dump("wait_accept", task=dep.label, state=dep.state.value)
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
        hub_cfg = config.load_hub()
        group_counts: dict[str, int] = {}
        for t in all_tasks:
            if t.id in busy:
                _role, m_list = quota.task_models(t)
                for m in m_list:
                    _prov, b_list = quota.get_model_buckets(self.store, m)
                    for b in b_list:
                        group_counts[b.group] = group_counts.get(b.group, 0) + 1
                        break
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
                    reason = reasons.dump("queue_paused")
                if not reason:
                    reason = self._deps_ok(t)
                if not reason and slots <= 0:
                    reason = reasons.dump("wait_slot", running=len(running), max=project.max_parallel)
                if not reason:
                    for r in t.limits.get("resources") or []:
                        spec = project.resources.get(r)
                        if spec is None:
                            reason = reasons.dump("no_resource", name=r)
                            break
                        if res_use.get(r, 0) >= spec.capacity:
                            reason = reasons.dump("wait_resource", name=r)
                            break
                        if spec.lock and self.lock_busy(spec.lock):
                            reason = reasons.dump("wait_resource_busy", name=r)
                            break
                if not reason:
                    qres = quota.check_quota_for_task(self.store, t, hub_cfg.quota, group_counts)
                    if not qres.ok:
                        if qres.fallback_model:
                            old_model = t.executor
                            if t.kind is Kind.REVIEW:
                                rev = dict(t.review)
                                models = list(rev.get("models") or [t.executor])
                                old_model = models[0] if models else t.executor
                                rev["models"] = [qres.fallback_model]
                                self.store.update_task(t.id, review=rev)
                            else:
                                self.store.update_task(t.id, executor=qres.fallback_model,
                                                       limits={**t.limits, "fresh_session": True})
                                t.executor = qres.fallback_model
                            self.store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                                                 payload={"from": old_model, "to": qres.fallback_model,
                                                          "text": qres.event_text})
                        else:
                            reason = qres.wait_reason
                if reason:
                    pl.waiting[t.id] = reasons.text(reason)
                    self._set_wait(t, reason)
                    continue
                self._set_wait(t, "")
                try:
                    pid = self.spawn(t.id)
                except OSError as e:
                    self.log.error("worker process T%d failed to start: %s", t.id, e, extra={"task": t.id})
                    pl.waiting[t.id] = reasons.text(reasons.dump("spawn_failed", err=e))
                    continue
                self.recent[t.id] = time.monotonic()
                spawned.append(t.id)
                running.append(t)
                slots -= 1
                for r in t.limits.get("resources") or []:
                    res_use[r] = res_use.get(r, 0) + 1
                _role, m_list = quota.task_models(t)
                for m in m_list:
                    _prov, b_list = quota.get_model_buckets(self.store, m)
                    for b in b_list:
                        group_counts[b.group] = group_counts.get(b.group, 0) + 1
                        break
                self.log.info("worker process T%d started (pid %s)", t.id, pid, extra={"task": t.id})
        self.store.meta_set(HEARTBEAT_KEY, str(now_ms()))
        return TickReport(live=live, spawned=spawned, load=load, paused=paused)

    def _orphans(self, live: dict[int, int], busy: set[int]) -> list[int]:
        """Active tasks with no process: back to queue (resume in place) or "Needs decision"."""
        now = now_ms()
        handled = []
        for t in self.store.list_tasks(states=ACTIVE):
            if t.id in busy or t.id in live:
                continue
            if t.state is State.ACCEPTING and t.owner_pid and procs.alive(t.owner_pid, self.proc_root) \
                    and accepting_task(t.owner_pid, t.id, self.proc_root):
                continue  # an `ahub accept` in progress — this pid is this task's accept (a live lease below)
            if t.owner and t.lease_until and t.lease_until + ORPHAN_GRACE_MS > now:
                continue  # lease (or its grace) still alive — the owner may be outside the task process (CLI)
            if not t.owner and now - t.updated_at < ORPHAN_GRACE_MS:
                continue  # just transitioned — the process may still appear
            token = f"service:{os.getpid()}"
            if not transitions.acquire(self.store, t.id, token, pid=os.getpid(), lease_ms=30_000):
                continue
            count = int(t.limits.get("orphans") or 0) + 1
            lim = dict(t.limits)
            lim["orphans"] = count
            self.store.update_task(t.id, limits=lim)
            try:
                if t.state is State.ACCEPTING:
                    to, reason = State.NEEDS_DECISION, reasons.dump("orphan_accepting", label=t.label)
                elif count > MAX_ORPHANS:
                    to, reason = State.NEEDS_DECISION, reasons.dump("orphan_repeat", n=count)
                else:
                    to, reason = State.QUEUED, reasons.dump("orphan_once")
                self.store.add_event(Ev.ORPHAN, task_id=t.id, project=t.project,
                                     payload={"from": t.state.value, "to": to.value, "count": count,
                                              "text": f"{t.label}: {reasons.text(reason)}"})
                transitions.move(self.store, t.id, to, reason=reason, by="service", owner=token)
                self.log.warning("orphan T%d (%s) → %s", t.id, t.state.value, to.value, extra={"task": t.id})
                handled.append(t.id)
            finally:
                transitions.release(self.store, t.id, token)
        return handled

    def stop(self) -> None:
        self._stop.set()

    def _observe(self) -> None:
        """Observer — on its own thread: model parsing must not stall the queue."""
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
                self.log.exception("observer crashed")

        self._obs = threading.Thread(target=_run, name="observer", daemon=True)
        self._obs.start()

    def run_forever(self, poll_s: float = 2.0, *, self_update: bool = True, observe: bool = True) -> None:
        from ahub import observer

        self.log.info("service started (pid %d)", os.getpid())
        code0 = code_fingerprint()
        last_check = time.monotonic()
        self._obs = None

        def _sig(signum, frame):
            self.log.info("signal %d — stopping (task processes keep running)", signum)
            self._stop.set()

        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(s, _sig)
            except ValueError:  # not the main thread (tests)
                pass
        while not self._stop.is_set():
            try:
                self.tick()
                if observe:
                    self._observe()
                    observer.watchdog(self.store)
            except Exception:
                self.log.exception("service tick failed")
            if self_update and time.monotonic() - last_check >= CODE_CHECK_S:
                last_check = time.monotonic()
                code = code_fingerprint()
                if code != code0:
                    ok, why = new_code_healthy()
                    if ok:
                        self.log.info("hub code changed — restarting service on new code (tasks untouched)")
                        restart_self()
                    else:
                        self.log.error("hub code changed but fails check — staying on old: %s", why)
                        code0 = code  # do not re-check every 10 s; the next change will be checked again
            self._stop.wait(poll_s)
        self.log.info("service stopped")
