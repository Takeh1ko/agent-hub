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
    """HEAD worktree через `git rev-parse HEAD`. None при ошибке/таймауте."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    sha = r.stdout.strip()
    return sha or None


def _shas_equal(head: str, base: str) -> bool:
    """Равенство sha с учётом короткого (prefix при длине ≥ 7)."""
    h = (head or "").strip()
    b = (base or "").strip()
    if not h or not b:
        return False
    if h == b:
        return True
    if len(h) >= 7 and len(b) >= 7 and (h.startswith(b) or b.startswith(h)):
        return True
    return False


def _lock_time_text(started_ms: int) -> str:
    """Время держателя замка коротко (локальная зона)."""
    try:
        from hub import time as ht

        return ht.fmt_local(started_ms)
    except Exception:
        return str(started_ms)


def _write_rules_sha(store: Store, task_id: str, digest: str) -> tuple[bool, str]:
    """Записать sha256 правил только через `store.upsert_task` (арбитр №6).

    Схема — миграция `005_rules_sha.sql`, колонка в `TASK_COLUMNS`.
    Никаких ALTER из кода.
    """
    try:
        store.upsert_task(id=task_id, rules_sha=digest)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        return False, f"store-fail: {e}"[-2000:]
    return True, ""


def preflight(
    store: Store,
    task_id: str,
    project: ProjectConfig,
    proc_root: str = "/proc",
) -> PreflightResult:
    """Проверки по порядку карточки; первая неуспешная — итог; никогда не бросает."""
    try:
        task = store.get_task(task_id)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        return PreflightResult(ok=False, reason=f"store-fail: {e}"[-2000:])
    if task is None:
        return PreflightResult(ok=False, reason="no-task")
    worktree = str(task.get("worktree") or "")
    wt = Path(worktree) if worktree else None
    if wt is None or not wt.is_dir():
        return PreflightResult(ok=False, reason="dirty")
    try:
        dirty = is_dirty(worktree)
    except (OSError, subprocess.SubprocessError):
        # Fail-safe из hub/read/git.py: ошибка git — считаем грязным.
        return PreflightResult(ok=False, reason="dirty")
    if dirty:
        return PreflightResult(ok=False, reason="dirty")
    base_sha = str(task.get("base_sha") or "")
    head = _head_sha(worktree)
    if not base_sha or head is None or not _shas_equal(head, base_sha):
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
        try:
            code, out_tail, err_tail = run_hook(hook, env, Path(worktree))
        except (OSError, subprocess.SubprocessError) as e:
            return PreflightResult(ok=False, reason=f"setup-fail: {e}"[-2000:])
        if code != 0:
            raw = err_tail.strip() or out_tail.strip()
            one = " ".join(raw.split())
            tail = one[-2000:] if len(one) > 2000 else one
            if not tail:
                tail = f"код {code}"
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
    except (OSError, subprocess.SubprocessError):
        return PreflightResult(ok=False, reason="collect-fail")
    if r.returncode != 0:
        return PreflightResult(ok=False, reason="collect-fail")
    lock_path = (project.test_lock or "").strip()
    if lock_path:
        try:
            holder = lock_holder(lock_path, proc_root)
        except (OSError, subprocess.SubprocessError) as e:
            return PreflightResult(ok=False, reason=f"lock-fail: {e}"[-2000:])
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
