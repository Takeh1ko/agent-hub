"""Процессы: дети, живость, cmdline, время старта (для сторожа тишины и пульса).

Linux — через /proc, иначе — psutil (macOS). Нет ни того ни другого — «нет данных».
"""

from __future__ import annotations

from pathlib import Path


def pids(proc_root: str | Path = "/proc") -> list[int]:
    """Все pid процессов."""
    if Path(proc_root).is_dir():
        try:
            return [int(e.name) for e in Path(proc_root).iterdir() if e.name.isdigit()]
        except OSError:
            return []
    try:
        import psutil
    except ImportError:
        return []
    try:
        return list(psutil.pids())
    except Exception:
        return []


def children(pid: int | None, proc_root: str | Path = "/proc") -> list[int]:
    """Прямые дети процесса (по всем его потокам)."""
    if pid is None:
        return []
    if Path(proc_root).is_dir():
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
    try:
        import psutil
    except ImportError:
        return []
    try:
        return [c.pid for c in psutil.Process(int(pid)).children(recursive=False)]
    except Exception:
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
    if Path(proc_root).is_dir():
        try:
            stat = (Path(proc_root) / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        try:
            state = stat.rsplit(")", 1)[1].split()[0]
        except IndexError:
            return True
        return state != "Z"
    try:
        import psutil
    except ImportError:
        return False
    try:
        p = psutil.Process(int(pid))
        if not p.is_running():
            return False
        return p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True
    except Exception:
        return False


def cmdline(pid: int, proc_root: str | Path = "/proc") -> list[str]:
    if Path(proc_root).is_dir():
        try:
            raw = (Path(proc_root) / str(int(pid)) / "cmdline").read_bytes()
        except OSError:
            return []
        return [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    try:
        import psutil
    except ImportError:
        return []
    try:
        return list(psutil.Process(int(pid)).cmdline())
    except Exception:
        return []


def start_time(pid: int, proc_root: str | Path = "/proc") -> int | None:
    """Время старта процесса — отличает процесс от нового с тем же pid (сравнение только на равенство)."""
    if Path(proc_root).is_dir():
        try:
            stat = (Path(proc_root) / str(int(pid)) / "stat").read_text(encoding="utf-8", errors="replace")
            return int(stat.rsplit(")", 1)[1].split()[19])
        except (OSError, IndexError, ValueError):
            return None
    try:
        import psutil
    except ImportError:
        return None
    try:
        return int(psutil.Process(int(pid)).create_time() * 100)
    except Exception:
        return None
