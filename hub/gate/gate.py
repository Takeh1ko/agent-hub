"""Ворота: непустой дифф внутри allowed, зелёная приёмка под замком."""

from __future__ import annotations

import fcntl
import time
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


def _rev_count(repo: str, base: str, head: str) -> tuple[int | None, str]:
    """Число коммитов base..head; при ошибке git — (None, последняя строка stderr)."""
    r = _git(repo, "rev-list", "--count", f"{base}..{head}", "--")
    if r.returncode != 0:
        return None, _git_err(r)
    try:
        return int(r.stdout.strip()), ""
    except ValueError:
        return None, "rev-list вернул не число"


def _diff_names(repo: str, base: str, head: str) -> tuple[list[str] | None, str]:
    """Файлы base..head; при ошибке git — (None, последняя строка stderr).

    --no-renames: перенос показывает и старый путь (иначе удаление вне
    allowed проходит ворота), -c core.quotepath=false: не-ASCII без кавычек.
    """
    r = _git(repo, "-c", "core.quotepath=false", "diff", "--no-renames",
             "--name-only", f"{base}..{head}", "--")
    if r.returncode != 0:
        return None, _git_err(r)
    return [l for l in (s.strip() for s in r.stdout.splitlines()) if l], ""


def _git_err(r: subprocess.CompletedProcess[str]) -> str:
    err = (r.stderr or "").strip().splitlines()
    return err[-1] if err else f"код {r.returncode}"


def _dirty_paths(repo: str) -> list[str] | None:
    """Незакоммиченное/неотслеженное вне .agent/ и .agent.prev_*/; None — git не ответил.

    .agent.prev_<ts>/ создаёт сам харнесс (continue/repair-логи) — не грязь исполнителя.
    Формат -z: токены по \\0 без кавычек, у переименований второй путь следующим токеном.
    """
    r = _git(repo, "-c", "core.quotepath=false", "status", "--porcelain", "-z")
    if r.returncode != 0:
        return None
    raw: list[str] = []
    toks = [t for t in r.stdout.split("\0") if t]
    i = 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if len(tok) > 3 and tok[2] == " ":
            raw.append(tok[3:])
            if tok[0] in "RC" and i < len(toks):
                # Переименование/копия в -z: второй путь следующим токеном.
                raw.append(toks[i])
                i += 1
        else:
            raw.append(tok)
    out: list[str] = []
    for p in raw:
        p = p.strip()
        if not p or p == ".agent" or p.startswith((".agent/", ".agent.prev_")):
            continue
        if p not in out:
            out.append(p)
    return out


def _diff_stat(repo: str, base: str, head: str) -> str:
    r = _git(repo, "-c", "core.quotepath=false", "diff", "--stat", f"{base}..{head}", "--")
    return r.stdout.strip() if r.returncode == 0 else ""


def _tail(text: str, limit: int = TAIL_LIMIT) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def _with_changed_tests(cmd: list[str], names: list[str], repo: str) -> list[str]:
    """Приёмка карточки + все тестовые файлы, изменённые задачей (иначе красный тест вне путей приёмки
    проскакивает). Команда без явных путей (весь набор) — как есть."""
    if "pytest" not in cmd:
        return cmd
    args = cmd[cmd.index("pytest") + 1:]
    given = [a for a in args if not a.startswith("-")]
    if not given:
        return cmd
    covered = [g.split("::")[0].rstrip("/") for g in given]
    for n in names:
        base = n.rsplit("/", 1)[-1]
        if not (base.startswith("test_") and base.endswith(".py")):
            continue
        if not Path(repo, n).exists():
            continue
        if any(n == c or n.startswith(c + "/") for c in covered):
            continue
        cmd.append(n)
        covered.append(n)
    return cmd


def check_gate(
    repo: Path,
    base_sha: str,
    head: str = "HEAD",
    allowed: list[str] | None = None,
    test_cmd: list[str] | None = None,
    lock_path: str | None = None,
    timeout_s: int = 1800,  # на сам прогон приёмки
    lock_wait_s: int | None = None,  # ожидание общего замка тестов; None — как timeout_s
    lock_poll_s: float = 5.0,
) -> GateResult:
    """Проверить ворота по порядку: чистота, дифф, allowed, приёмка под замком.

    Незакоммиченное/неотслеженное вне .agent/ и .agent.prev_*/ → dirty
    (приёмку не запускаем): иначе conftest.py/pytest.ini меняют саму приёмку мимо диффа.
    """
    repo_s = str(repo)
    allowed_list = list(allowed) if allowed else []
    if test_cmd is None:
        cmd: list[str] = [sys.executable, "-m", "pytest", "-q"]
    else:
        cmd = list(test_cmd)
    errors: list[str] = []

    dirty = _dirty_paths(repo_s)
    if dirty is None:
        return GateResult(ok=False, errors=["git-error: git status не сработал"],
                          diff_stat="", tests_tail="")
    for p in dirty:
        errors.append(f"dirty: {p}")
    if errors:
        return GateResult(ok=False, errors=errors,
                          diff_stat=_diff_stat(repo_s, base_sha, head), tests_tail="")

    count, count_err = _rev_count(repo_s, base_sha, head)
    if count is None:
        return GateResult(ok=False, errors=[f"git-error: {count_err}"],
                          diff_stat="", tests_tail="")
    if count <= 0:
        errors.append("empty-diff")
        return GateResult(ok=False, errors=errors,
                          diff_stat=_diff_stat(repo_s, base_sha, head), tests_tail="")

    names, names_err = _diff_names(repo_s, base_sha, head)
    if names is None:
        return GateResult(ok=False, errors=[f"git-error: {names_err}"],
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
    if errors:
        # Итог уже не-ok: замок не захватываем, приёмку не гоняем.
        return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")

    if not cmd:
        errors.append("tests-fail: пустая команда приёмки")
        return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")
    cmd = _with_changed_tests(list(cmd), names, repo_s)

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
        # Замок общий на проект: ждём его в пределах таймаута ворот (раньше — мгновенный отказ,
        # и при нескольких параллельных задачах ворота валили почти всех, 2026-09-29).
        if lock_wait_s is None:
            lock_wait_s = int(os.environ.get("HUB_GATE_LOCK_WAIT_S", timeout_s))
        deadline = time.monotonic() + max(int(lock_wait_s), 0)
        while True:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    holder = lock_holder(str(lock_path))
                    errors.append(f"locked: pid {holder.pid}" if holder is not None else "locked: pid ?")
                    os.close(lock_fd)
                    return GateResult(ok=False, errors=errors, diff_stat=stat, tests_tail="")
                time.sleep(lock_poll_s)

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
