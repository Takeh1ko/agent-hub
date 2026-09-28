"""Процессы агентов по /proc. Путь /proc — параметром (фейк в тестах)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Proc:
    pid: int
    kind: str  # opencode|agy|run_task|pytest|flock|hub_bot
    cwd: str
    args: list[str]
    started_ms: int
    children: list[int] = field(default_factory=list)


def _kind_of(args: list[str]) -> str | None:
    # Имя бинарника важнее подстрок в путях (каталог может содержать «pytest»).
    base = Path(args[0]).name if args else ""
    blob = "\x00".join(args)
    if base == "flock" or base.startswith("flock"):
        return "flock"
    if "run_task" in blob:
        return "run_task"
    # hub_bot (H07): живой `hub bot` — консольный скрипт hub с подкомандой bot
    # либо `python -m hub.bot…` / `python -m hub.cli bot`. Другие подкоманды hub
    # (status, stop…) — короткие CLI-вызовы, не агенты: kind None, в список не идут.
    stem = Path(args[0]).stem if args else ""
    if stem == "hub":
        if "bot" in args[1:]:
            return "hub_bot"
        return None
    if "-m" in args and any(a == "hub.cli" or a.startswith("hub.") for a in args):
        if "bot" in blob:
            return "hub_bot"
    if base == "opencode" or base.startswith("opencode"):
        return "opencode"
    if base == "agy" or base.startswith("agy"):
        return "agy"
    if "pytest" in blob:
        return "pytest"
    if "flock" in blob:
        return "flock"
    return None


def _ppid(stat_text: str) -> int | None:
    """4-е поле stat (после (comm))."""
    try:
        tail = stat_text[stat_text.rindex(")") + 1:].split()
        return int(tail[1])
    except (ValueError, IndexError):
        return None


def agent_procs(proc_root: str | Path = "/proc") -> list[Proc]:
    """Все процессы агентов. Исчезнувшие/нечитаемые pid пропускаются."""
    root = Path(proc_root)
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    infos: dict[int, dict] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        args = [a for a in raw.split("\x00") if a]
        if not args:
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            cwd = ""
        try:
            st = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            ppid = _ppid(st)
        except OSError:
            ppid = None
        try:
            started = int((entry / "stat").stat().st_mtime * 1000)
        except OSError:
            started = 0
        infos[pid] = {"args": args, "cwd": cwd, "ppid": ppid, "started": started}
    kids: dict[int, list[int]] = {pid: [] for pid in infos}
    for pid, info in infos.items():
        ppid = info["ppid"]
        if ppid in kids:
            kids[ppid].append(pid)
    out: list[Proc] = []
    for pid, info in sorted(infos.items()):
        kind = _kind_of(info["args"])
        if kind is None:
            continue
        out.append(Proc(
            pid=pid,
            kind=kind,
            cwd=info["cwd"],
            args=info["args"],
            started_ms=info["started"],
            children=sorted(kids.get(pid, [])),
        ))
    return out


def lock_holder(lock_path: str, proc_root: str | Path = "/proc") -> Proc | None:
    """Процесс, держащий замок (flock с путём замка в аргументах)."""
    needle = str(lock_path)
    cands = [p for p in agent_procs(proc_root) if needle in " ".join(p.args)]
    if not cands:
        return None
    for p in cands:
        if p.kind == "flock":
            return p
    return cands[0]
