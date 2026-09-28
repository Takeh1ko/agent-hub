"""Предполётные проверки окружения (§7 spec). Первая неуспешная проверка — итог."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from hub.config import ProjectConfig
from hub.read.git import is_dirty
from hub.read.procs import lock_holder
from hub.store import Store


@dataclass
class PreflightResult:
    ok: bool
    reason: str = field(default="")


def run_hook(
    cmd: str,
    env: dict[str, str],
    cwd: Path,
    timeout_s: int = 120,
) -> tuple[int, str, str]:
    """Запустить shell-хук; вернуть (код, stdout-хвост ≤2000, stderr-хвост ≤2000)."""
    full_env = dict(os.environ)
    full_env.update(env)
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=str(cwd),
            env=full_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        out = (e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or ""))[-2000:]
        err = (e.stderr.decode() if isinstance(e.stderr, bytes) else (e.stderr or ""))[-2000:]
        tail = (err or out or "timeout").strip()[-2000:]
        return (124, out[-2000:], (tail or "timeout")[-2000:])
    except OSError as e:
        return (127, "", str(e)[-2000:])
    out = r.stdout if isinstance(r.stdout, str) else ""
    err = r.stderr if isinstance(r.stderr, str) else ""
    return (r.returncode, out[-2000:], err[-2000:])


def _rules_path(project: ProjectConfig) -> Path:
    """Путь к rules: абсолютный или относительно root проекта."""
    p = Path(project.rules) if project.rules else Path("")
    if str(p) and p.is_absolute():
        return p
    root = project.root.strip() if project.root else ""
    if root and str(p):
        return Path(root) / p
    return p


def _head_sha(worktree: str) -> str | None:
    """HEAD worktree через `git rev-parse HEAD`. None при ошибке."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _lock_time_text(started_ms: int) -> str:
    """Время держателя замка коротко (локальная зона)."""
    try:
        from hub import time as ht

        return ht.fmt_local(started_ms)
    except Exception:
        return str(started_ms)


def _write_rules_sha(store: Store, task_id: str, digest: str) -> tuple[bool, str]:
    """Записать sha256 правил прямым SQL (без мутации hub.store).

    Возвращает (успех, причина). Без файлов миграций: колонка добавляется
    при отсутствии; контракт «через upsert» невыполним без правки
    hub/store.py + миграции (см. отчёт) — пишем явным UPDATE.
    """
    try:
        con = sqlite3.connect(str(store.path))
    except (OSError, sqlite3.Error) as e:
        return False, f"store-fail: {e}"[-2000:]
    try:
        try:
            con.execute("ALTER TABLE task ADD COLUMN rules_sha TEXT NOT NULL DEFAULT ''")
            con.commit()
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                return False, f"store-fail: {e}"[-2000:]
        try:
            cur = con.execute("UPDATE task SET rules_sha=? WHERE id=?", (digest, task_id))
            con.commit()
            if cur.rowcount == 0:
                return False, "store-fail: нет задачи"[-2000:]
        except (OSError, sqlite3.Error) as e:
            return False, f"store-fail: {e}"[-2000:]
    finally:
        try:
            con.close()
        except (OSError, sqlite3.Error):
            pass
    return True, ""


def preflight(
    store: Store,
    task_id: str,
    project: ProjectConfig,
    proc_root: str = "/proc",
) -> PreflightResult:
    """Проверки по порядку карточки; первая неуспешная — итог."""
    task = store.get_task(task_id)
    if task is None:
        return PreflightResult(ok=False, reason="no-task")
    worktree = str(task.get("worktree") or "")
    wt = Path(worktree) if worktree else None
    if wt is None or not wt.is_dir() or is_dirty(worktree):
        return PreflightResult(ok=False, reason="dirty")
    base_sha = str(task.get("base_sha") or "")
    head = _head_sha(worktree)
    if not base_sha or head is None or head != base_sha:
        return PreflightResult(ok=False, reason="base-moved")
    rp = _rules_path(project)
    if not str(rp) or not rp.is_file():
        return PreflightResult(ok=False, reason="no-rules")
    hook = (project.hooks.task_setup or "").strip() if project.hooks else ""
    if hook:
        env = {
            "HUB_TASK_ID": task_id,
            "HUB_WORKTREE": worktree,
            "HUB_PROJECT_ROOT": project.root or "",
        }
        code, out_tail, err_tail = run_hook(hook, env, Path(worktree))
        if code != 0:
            raw = err_tail.strip() or out_tail.strip()
            one = " ".join(raw.split())
            tail = one[-2000:] if len(one) > 2000 else one
            return PreflightResult(ok=False, reason=f"setup-fail: {tail}")
    # `pytest --collect-only` питоном проекта в worktree.
    # Заданный, но отсутствующий питон — collect-fail (не молчаливый fallback).
    python = (project.python or "").strip()
    if python:
        if not Path(python).exists():
            return PreflightResult(ok=False, reason="collect-fail")
        py = python
    else:
        py = sys.executable
    try:
        r = subprocess.run(
            [py, "-m", "pytest", "--collect-only", "-q"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PreflightResult(ok=False, reason="collect-fail")
    if r.returncode != 0:
        return PreflightResult(ok=False, reason="collect-fail")
    lock_path = (project.test_lock or "").strip()
    if lock_path:
        holder = lock_holder(lock_path, proc_root)
        if holder is not None:
            when = _lock_time_text(holder.started_ms)
            return PreflightResult(ok=False, reason=f"locked: pid {holder.pid} since {when}")
    # Всё чисто — записать sha256 правил (только при успехе всех проверок).
    try:
        digest = hashlib.sha256(rp.read_bytes()).hexdigest()
    except OSError:
        return PreflightResult(ok=False, reason="no-rules")
    ok_write, write_reason = _write_rules_sha(store, task_id, digest)
    if not ok_write:
        return PreflightResult(ok=False, reason=write_reason or "store-fail")
    return PreflightResult(ok=True, reason="")
