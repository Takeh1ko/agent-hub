"""Ворота: непустой дифф внутри allowed, зелёная приёмка под замком."""

from __future__ import annotations

import fcntl
import fnmatch
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from hub.read.procs import lock_holder

TAIL_LIMIT = 2000
_GIT_TIMEOUT = 60


@dataclass
class GateResult:
    ok: bool
    errors: list[str]
    diff_stat: str
    tests_tail: str = ""


def _git(repo: str, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo,
            capture_output=True, text=True, timeout=_GIT_TIMEOUT,
        )
    except OSError as e:
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=127, stdout="", stderr=str(e))
    except subprocess.TimeoutExpired as e:
        err = e.stderr if isinstance(e.stderr, str) else ""
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=124, stdout="", stderr=f"timeout: {err}")


def _rev_count(repo: str, base: str, head: str) -> int | None:
    r = _git(repo, "rev-list", "--count", f"{base}..{head}", "--")
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _diff_names(repo: str, base: str, head: str) -> list[str] | None:
    r = _git(repo, "diff", "--name-only", f"{base}..{head}", "--")
    if r.returncode != 0:
        return None
    return [l for l in (s.strip() for s in r.stdout.splitlines()) if l]


def _diff_stat(repo: str, base: str, head: str) -> str:
    r = _git(repo, "diff", "--stat", f"{base}..{head}", "--")
    return r.stdout.strip() if r.returncode == 0 else ""


def _tail(text: str, limit: int = TAIL_LIMIT) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def check_gate(
    repo: Path,
    base_sha: str,
    head: str = "HEAD",
    allowed: list[str] | None = None,
    test_cmd: list[str] | None = None,
    lock_path: str | None = None,
    timeout_s: int = 600,
) -> GateResult:
    """Проверить ворота по порядку: дифф, allowed, приёмка под замком."""
    repo_s = str(repo)
    allowed_list = list(allowed) if allowed else []
    if test_cmd is None:
        cmd: list[str] = [sys.executable, "-m", "pytest", "-q"]
    else:
        cmd = list(test_cmd)
    errors: list[str] = []

    count = _rev_count(repo_s, base_sha, head)
    if count is None or count <= 0:
        errors.append("empty-diff")
        return GateResult(ok=False, errors=errors,
                          diff_stat=_diff_stat(repo_s, base_sha, head), tests_tail="")

    names = _diff_names(repo_s, base_sha, head)
    if names is None:
        errors.append("empty-diff")
        return GateResult(ok=False, errors=errors,
                          diff_stat=_diff_stat(repo_s, base_sha, head), tests_tail="")
    if not names:
        # Коммиты есть, но файлов нет (allow-empty): работы нет.
        errors.append("empty-diff")
        return GateResult(ok=False, errors=errors,
                          diff_stat=_diff_stat(repo_s, base_sha, head), tests_tail="")
    for path in names:
        if not any(fnmatch.fnmatch(path, pat) for pat in allowed_list):
            errors.append(f"forbidden: {path}")
    stat = _diff_stat(repo_s, base_sha, head)

    if not cmd:
        errors.append("tests-fail: пустая команда приёмки")
        return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")

    lock_fd: int | None = None
    if lock_path:
        try:
            Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as e:
            errors.append(f"lock-error: {e}")
            return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            holder = lock_holder(str(lock_path))
            if holder is not None:
                errors.append(f"locked: pid {holder.pid}")
            else:
                errors.append("locked: pid ?")
            os.close(lock_fd)
            return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")

    try:
        try:
            r = subprocess.run(cmd, cwd=repo_s, capture_output=True,
                               text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired as e:
            out_s = e.stdout if isinstance(e.stdout, str) else ""
            err_s = e.stderr if isinstance(e.stderr, str) else ""
            tail = _tail((out_s + "\n" + err_s) if out_s and err_s else (out_s + err_s))
            if tail:
                errors.append(f"tests-fail: timeout {timeout_s}с: {tail}")
            else:
                errors.append(f"tests-fail: timeout {timeout_s}с")
            return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail=tail)
        except OSError as e:
            errors.append(f"tests-fail: не запустилась: {e}")
            return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")
        out = r.stdout or ""
        err = r.stderr or ""
        tail = _tail((out + "\n" + err) if out and err else (out + err))
        if r.returncode != 0:
            errors.append(f"tests-fail: {tail}" if tail else f"tests-fail: код {r.returncode}")
            return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail=tail)
        return GateResult(ok=not errors, errors=errors, diff_stat=stat, tests_tail=tail)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(lock_fd)
