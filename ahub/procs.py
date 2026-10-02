"""Процессы: дети, живость, cmdline, время старта (для сторожа тишины и пульса).

Linux — через /proc, иначе — psutil (macOS). Нет ни того ни другого — «нет данных».
"""

from __future__ import annotations

from pathlib import Path


def _proc_dir(proc_root: str | Path) -> Path | None:
    """Каталог /proc, если он есть (Linux и тесты с подменным корнем); иначе None — идём через psutil."""
    root = Path(proc_root)
    return root if root.is_dir() else None


def _psutil():
    """Модуль psutil или None (не установлен — «нет данных»)."""
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def pids(proc_root: str | Path = "/proc") -> list[int]:
    """Все pid процессов."""
    root = _proc_dir(proc_root)
    if root is not None:
        try:
            return [int(e.name) for e in root.iterdir() if e.name.isdigit()]
        except OSError:
            return []
    ps = _psutil()
    if ps is None:
        return []
    return list(ps.pids())


def children(pid: int | None, proc_root: str | Path = "/proc") -> list[int]:
    """Прямые дети процесса (по всем его потокам)."""
    if pid is None:
        return []
    root = _proc_dir(proc_root)
    if root is not None:
        out: list[int] = []
        taskdir = root / str(int(pid)) / "task"
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
    ps = _psutil()
    if ps is None:
        return []
    try:
        return [c.pid for c in ps.Process(int(pid)).children()]
    except ps.Error:
        return []


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
    root = _proc_dir(proc_root)
    if root is not None:
        try:
            stat = (root / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        try:
            state = stat.rsplit(")", 1)[1].split()[0]
        except IndexError:
            return True
        return state != "Z"
    ps = _psutil()
    if ps is None:
        return False
    try:
        return ps.Process(int(pid)).status() != ps.STATUS_ZOMBIE
    except ps.AccessDenied:
        return True  # чужой процесс: есть, но статус не виден
    except ps.Error:
        return False


def cmdline(pid: int, proc_root: str | Path = "/proc") -> list[str]:
    root = _proc_dir(proc_root)
    if root is not None:
        try:
            raw = (root / str(int(pid)) / "cmdline").read_bytes()
        except OSError:
            return []
        return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    ps = _psutil()
    if ps is None:
        return []
    try:
        return ps.Process(int(pid)).cmdline()
    except ps.Error:
        return []


def start_time(pid: int, proc_root: str | Path = "/proc") -> int | None:
    """Время старта процесса — отличает процесс от нового с тем же pid (сравнение только на равенство)."""
    root = _proc_dir(proc_root)
    if root is not None:
        try:
            stat = (root / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
            return int(stat.rsplit(")", 1)[1].split()[19])
        except (OSError, IndexError, ValueError):
            return None
    ps = _psutil()
    if ps is None:
        return None
    try:
        return int(ps.Process(int(pid)).create_time() * 100)
    except ps.Error:
        return None
