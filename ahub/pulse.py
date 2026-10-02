"""Пульс задач (V19, architecture §7): доказательство жизни из нескольких источников.

🟢 работает — свежая активность (поставщик/лог сессии) · 🟡 ждёт по делу — нет активности, но идёт инструмент,
тесты (дети процесса), ожидание замка/ресурса/паузы · 🔴 молчит — ни активности, ни объяснения дольше порога фазы ·
⚫ мёртв — процесса задачи нет, а задача активна · ⚪ нет данных — поставщик не даёт сигнала.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ahub import procs, providers
from ahub.config import ProjectConfig
from ahub.i18n import t as _t
from ahub.model import ACTIVE, State
from ahub.store import Store, Task
from ahub.time import now_ms

FRESH_MS = 2 * 60_000
THRESHOLD_MS = {  # молчание без объяснения дольше — 🔴
    State.PREPARING: 15 * 60_000, State.WORKING: 20 * 60_000, State.FIXING: 20 * 60_000,
    State.CHECKING: 30 * 60_000, State.REVIEWING: 15 * 60_000, State.ACCEPTING: 30 * 60_000,
}
MARKS = {"working": "🟢", "waiting": "🟡", "silent": "🔴", "dead": "⚫", "unknown": "⚪"}


@dataclass
class Pulse:
    task_id: int
    state: str  # working | waiting | silent | dead | unknown
    reason: str = ""
    last_activity_ms: int | None = None
    active_tool: str = ""
    pid: int | None = None

    @property
    def mark(self) -> str:
        return MARKS[self.state]


def lock_holders(proc_root: str | Path = "/proc") -> dict[int, list[int]]:
    """inode → pid'ы держателей flock/posix-замков (по /proc/locks)."""
    out: dict[int, list[int]] = {}
    try:
        text = (Path(proc_root) / "locks").read_text()
    except OSError:
        return out
    for line in text.splitlines():
        parts = line.split()
        if "->" in parts:  # ожидающие — не держатели
            continue
        try:
            pid = int(parts[4])
            inode = int(parts[5].split(":")[2])
        except (IndexError, ValueError):
            continue
        out.setdefault(inode, []).append(pid)
    return out


def lock_holder(path: str, proc_root: str | Path = "/proc") -> int | None:
    try:
        ino = os.stat(path).st_ino
    except OSError:
        return None
    pids = lock_holders(proc_root).get(ino) or []
    return pids[0] if pids else None


def _describe(pid: int, proc_root) -> str:
    args = procs.cmdline(pid, proc_root)
    return " ".join(args)[:60] if args else f"pid {pid}"


def task_pulse(store: Store, t: Task, *, live: dict[int, int], project: ProjectConfig | None = None,
               now: int | None = None, proc_root: str | Path = "/proc") -> Pulse:
    ts = now if now is not None else now_ms()
    pid = live.get(t.id)
    if pid is None:
        return Pulse(t.id, "dead", _t("pulse.dead"), pid=None)
    # Последняя сессия, которая идёт сейчас (или последняя вообще).
    sessions = store.list_sessions(t.id)
    running = [s for s in sessions if s.status == "running"]
    s = running[-1] if running else (sessions[-1] if sessions else None)
    last = t.updated_at
    tool, tool_since = "", None
    known = False
    if s is not None:
        if s.log_path and Path(s.log_path).exists():
            last = max(last, int(Path(s.log_path).stat().st_mtime * 1000))
            known = True
        if s.external_id:
            try:
                st = providers.get(s.provider).session_state(s.external_id)
            except (KeyError, Exception):
                st = None
            if st is not None:
                known = True
                if st.last_activity_ms:
                    last = max(last, st.last_activity_ms)
                tool, tool_since = st.active_tool, st.tool_started_ms
    age = ts - last
    kids = procs.descendants(s.pid, proc_root) if s is not None and s.pid else []
    if age < FRESH_MS:
        return Pulse(t.id, "working", "", last, tool, pid)
    if tool:
        mins = (ts - tool_since) // 60000 if tool_since else age // 60000
        return Pulse(t.id, "waiting", _t("pulse.tool", tool=tool, mins=mins), last, tool, pid)
    if t.phase == "waiting":
        why = t.state_reason or _t("pulse.waiting")
        if project is not None and project.test_resource in project.resources:
            lk = project.resources[project.test_resource].lock
            holder = lock_holder(lk, proc_root) if lk else None
            if holder:
                why = _t("pulse.test_lock", holder=_describe(holder, proc_root))
        return Pulse(t.id, "waiting", why, last, "", pid)
    if kids:
        return Pulse(t.id, "waiting", _t("pulse.children", desc=_describe(kids[0], proc_root)), last, "", pid)
    if not known:
        return Pulse(t.id, "unknown", _t("pulse.no_signal"), last, "", pid)
    limit = THRESHOLD_MS.get(t.state, 20 * 60_000)
    if age >= limit:
        return Pulse(t.id, "silent", _t("pulse.silent", mins=age // 60000, limit=limit // 60000), last, "", pid)
    return Pulse(t.id, "working", _t("pulse.quiet", mins=age // 60000), last, "", pid)


def all_pulses(store: Store, *, live: dict[int, int] | None = None, projects: list[ProjectConfig] | None = None,
               now: int | None = None) -> dict[int, Pulse]:
    from ahub.service import live_workers

    live = live if live is not None else live_workers()
    by_name = {p.name: p for p in projects or []}
    return {t.id: task_pulse(store, t, live=live, project=by_name.get(t.project), now=now)
            for t in store.list_tasks(states=ACTIVE)}
