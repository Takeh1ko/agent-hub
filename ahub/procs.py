"""Процессы через /proc: дети процесса (для сторожа тишины и пульса). Нет /proc — «детей нет»."""

from __future__ import annotations

from pathlib import Path


def children(pid: int | None, proc_root: str | Path = "/proc") -> list[int]:
    """Прямые дети процесса (по всем его потокам)."""
    if pid is None:
        return []
    out: list[int] = []
    taskdir = Path(proc_root) / str(int(pid)) / "task"
    try:
        tids = list(taskdir.iterdir())
    except OSError:
        return []
    for tid in tids:
        try:
            txt = (tid / "children").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.extend(int(x) for x in txt.split() if x.isdigit())
    return out


def descendants(pid: int | None, proc_root: str | Path = "/proc", limit: int = 500) -> list[int]:
    """Все потомки (обход в ширину, с защитой от циклов)."""
    seen: list[int] = []
    queue = children(pid, proc_root)
    while queue and len(seen) < limit:
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.append(cur)
        queue.extend(children(cur, proc_root))
    return seen


def has_children(pid: int | None, proc_root: str | Path = "/proc") -> bool:
    return bool(children(pid, proc_root))


def alive(pid: int | None, proc_root: str | Path = "/proc") -> bool:
    """Процесс существует и не зомби."""
    if pid is None:
        return False
    try:
        stat = (Path(proc_root) / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        state = stat.rsplit(")", 1)[1].split()[0]
    except IndexError:
        return True
    return state != "Z"


def cmdline(pid: int, proc_root: str | Path = "/proc") -> list[str]:
    try:
        raw = (Path(proc_root) / str(int(pid)) / "cmdline").read_bytes()
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]


def start_time(pid: int, proc_root: str | Path = "/proc") -> int | None:
    """Время старта процесса (такты с загрузки) — отличает процесс от нового с тем же pid."""
    try:
        stat = (Path(proc_root) / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None
