"""Ворота задач, меняющих файлы (V13, architecture §5–§6; перенос правил hub/gate/gate.py v1).

База диффа — merge-base рабочей ветки и HEAD задачи: одна и та же для ревью, ворот и слияния (урок v1 H14b r3:
после подтягивания рабочей ветки в задачу ревьюер не должен видеть чужие файлы).

Проверки:
- есть коммит от базы; нет незакоммиченных изменений (кроме .ahub/)          → чинится одним repair;
- дифф ⊆ разрешённых файлов задачи                                              → не чинится: «Нужно решение»;
- .ahub/result.json: commit == HEAD, files ⊆ дифф (кроме правки оркестратора)  → чинится repair;
- код: приёмка зелёная под ресурсом тестов проекта                             → красная — доработка.
"""

from __future__ import annotations

import fcntl
import fnmatch
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ahub import archive, workspace
from ahub.config import ProjectConfig
from ahub.model import Kind
from ahub.prepare import scrub_env
from ahub.store import Task

TEST_TIMEOUT_S = 30 * 60
LOCK_WAIT_S = 30 * 60
TAIL_LINES = 15


@dataclass
class GateResult:
    base: str
    head: str
    repairable: list[str] = field(default_factory=list)
    fatal: list[str] = field(default_factory=list)
    diff_files: list[str] = field(default_factory=list)
    diffstat: str = ""
    tests_ok: bool | None = None
    tests_tail: str = ""
    tests_cmd: str = ""

    @property
    def ok(self) -> bool:
        return not self.repairable and not self.fatal and self.tests_ok is not False

    def summary(self) -> str:
        parts = [self.diffstat or "дифф пуст"]
        if self.tests_ok is not None:
            parts.append("приёмка зелёная" if self.tests_ok else "приёмка красная")
        return "; ".join(parts)


def effective_base(project: ProjectConfig, task: Task) -> str:
    """merge-base рабочей ветки и HEAD задачи; нет — исходная база задачи."""
    r = workspace.git(task.worktree, "merge-base", project.work_branch, "HEAD", check=False)
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else task.base_sha


def diff_files(path: str, base: str) -> list[str]:
    r = workspace.git(path, "diff", "--name-only", f"{base}..HEAD")
    return [x for x in r.stdout.splitlines() if x.strip()]


def diff_text(path: str, base: str, limit: int = 200_000) -> str:
    r = workspace.git(path, "diff", f"{base}..HEAD", check=False)
    out = r.stdout
    return out if len(out) <= limit else out[:limit] + f"\n… дифф обрезан ({len(out)} байт)"


def allowed(file: str, globs: list[str]) -> bool:
    f = file.removeprefix("./")
    return any(fnmatch.fnmatch(f, g.removeprefix("./")) for g in globs)


def clear_pycache(root: str) -> None:
    import shutil

    for d in Path(root).rglob("__pycache__"):
        if ".git" in d.parts:
            continue
        shutil.rmtree(d, ignore_errors=True)


class LockTimeout(RuntimeError):
    pass


def with_lock(path: str, fn: Callable[[], object], *, wait_s: float = LOCK_WAIT_S,
              on_wait: Callable[[], None] | None = None, should_stop: Callable[[], bool] | None = None):
    """Выполнить fn под внешним flock (общий замок тестов проекта). Пусто — без замка."""
    if not path:
        return fn()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        deadline = time.monotonic() + wait_s
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waited and on_wait is not None:
                    on_wait()
                waited = True
                if time.monotonic() >= deadline:
                    raise LockTimeout(f"замок {path} занят дольше {int(wait_s)} с")
                if should_stop is not None and should_stop():
                    raise LockTimeout("остановлено во время ожидания замка")
                time.sleep(1.0)
        try:
            return fn()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run_acceptance(project: ProjectConfig, cwd: str, nodes: list[str], *, task_label: str = "",
                   on_wait: Callable[[], None] | None = None,
                   should_stop: Callable[[], bool] | None = None) -> tuple[bool, str, str]:
    """(зелёная?, хвост вывода, команда). Под ресурсом тестов проекта."""
    py = project.python or "python3"
    cmd = [py, "-m", "pytest", "-q", *nodes]
    env = scrub_env(dict(os.environ))
    env.update(AHUB_TASK_ID=task_label, AHUB_WORKTREE=cwd, AHUB_PROJECT_ROOT=project.root,
               PYTHONDONTWRITEBYTECODE="1")
    clear_pycache(cwd)  # устаревший .pyc (правка того же размера в ту же секунду) дал бы ложную зелёную
    lock = ""
    if project.test_resource and project.test_resource in project.resources:
        lock = project.resources[project.test_resource].lock

    def _run():
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"таймаут {TEST_TIMEOUT_S} с"
        except OSError as e:
            return False, f"pytest не запустился: {e}"
        tail = "\n".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-TAIL_LINES:])
        return r.returncode == 0, tail

    try:
        ok, tail = with_lock(lock, _run, on_wait=on_wait, should_stop=should_stop)
    except LockTimeout as e:
        return False, str(e), " ".join(cmd)
    return ok, tail, " ".join(cmd[2:])


def check(project: ProjectConfig, task: Task, *, run_tests: bool = True, orch_edit: bool = False,
          on_wait: Callable[[], None] | None = None,
          should_stop: Callable[[], bool] | None = None) -> GateResult:
    path = task.worktree
    base = effective_base(project, task)
    head = workspace.head(path)
    g = GateResult(base=base, head=head)
    if workspace.commits_since(path, base) == 0:
        g.repairable.append("нет коммита от базы")
    dirty = workspace.changed_files(path)
    if dirty:
        g.repairable.append("незакоммиченные изменения: " + ", ".join(dirty[:10]))
    g.diff_files = diff_files(path, base) if base else []
    stat = workspace.git(path, "diff", "--shortstat", f"{base}..HEAD", check=False).stdout.strip()
    g.diffstat = stat
    globs = list(task.limits.get("paths") or [])
    outside = [f for f in g.diff_files if not allowed(f, globs)]
    if outside:
        g.fatal.append("изменены файлы вне разрешённых: " + ", ".join(outside[:10]))
    if not orch_edit:
        res = archive.read_json(Path(path) / workspace.AHUB_DIR / "result.json")
        if not res:
            g.repairable.append("нет .ahub/result.json")
        else:
            if str(res.get("commit", ""))[:7] != head[:7] or not str(res.get("commit", "")).strip():
                g.repairable.append(f"result.json: commit {str(res.get('commit', ''))[:10] or '—'} ≠ HEAD {head[:10]}")
            files = res.get("files") or []
            extra = [f for f in files if f not in g.diff_files]
            if extra:
                g.repairable.append("result.json: files не из диффа: " + ", ".join(map(str, extra[:10])))
    if run_tests and task.kind is Kind.CODE and not g.repairable and not g.fatal:
        nodes = list(task.limits.get("accept") or [])
        if nodes:
            g.tests_ok, g.tests_tail, g.tests_cmd = run_acceptance(project, path, nodes, task_label=task.label,
                                                                   on_wait=on_wait, should_stop=should_stop)
    return g
