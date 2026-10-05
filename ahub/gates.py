"""Gates for file-changing tasks (V13, architecture §5–§6; port of hub/gate/gate.py v1 rules).

Diff base is the merge-base of the work branch and the task HEAD: same for review, gates, and merge (v1 lesson
H14b r3: after pulling the work branch into the task, the reviewer must not see foreign files).

Checks:
- a commit past base; no uncommitted changes (except .ahub/)                → fixed by one repair;
- diff ⊆ task allowed files                                                 → unfixable: "Needs decision";
- .ahub/result.json: commit == HEAD, files ⊆ diff (except orchestrator edit) → fixed by repair;
- code: acceptance green under the project test resource                    → red — rework.

The test resource lock is taken here and only while acceptance runs — that is why the queue does not hold it
for the whole task (tasks.py); tasks that name it in --resources still get whole-task exclusivity.
"""

from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ahub import reasons, workspace
from ahub.config import ProjectConfig
from ahub.i18n import t as _t
from ahub.i18n import template
from ahub.model import Kind
from ahub.prepare import scrub_env, task_env
from ahub.store import Task

TEST_TIMEOUT_S = 30 * 60
LOCK_WAIT_S = 30 * 60
TAIL_LINES = 15
DIFF_LIMIT = 200_000  # the diff a reviewer reads; over that it is cut, not the task
# the shape of .ahub/result.json — a problem an orchestrator's own edit over the result may cause
RESULT_JSON_CODES = frozenset({"result_commit", "result_files", "no_result", "result_json"})


class Problem(str):
    """A gate problem: the localized text (it is a str — prompts and summaries use it as is) plus the
    reason code and params, so what goes on the task is data, not translated text."""

    __slots__ = ("code", "params")

    def __new__(cls, text: str, code: str, params: dict) -> "Problem":
        p = super().__new__(cls, text)
        p.code = code
        p.params = params
        return p


def problem(code: str, **params) -> Problem:
    """One problem (a gate or a scout result): text for a prompt, code for the task reason.

    The text is the `gates.` key of the code; a scout problem has none and takes the `reason.` one —
    it is stored on the task as a reason, and the reason catalogue is what renders it.
    """
    key = "gates." + code if template("gates." + code) else "reason." + code
    return Problem(_t(key, **params), code, params)


def codes(items: list[str], prefix: str = "gate_") -> list[dict]:
    """The problems as sub-reasons (to store); plain text items are skipped."""
    return [reasons.part(prefix + p.code, **p.params) for p in items if isinstance(p, Problem)]


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
        parts = [self.diffstat or _t("task.diff_empty")]
        if self.tests_ok is not None:
            parts.append(_t("gates.accept_green") if self.tests_ok else _t("gates.accept_red"))
        return "; ".join(parts)


def effective_base(project: ProjectConfig, task: Task) -> str:
    """merge-base of the work branch and the task HEAD; fallback — the task's original base."""
    r = workspace.git(task.worktree, "merge-base", project.work_branch, "HEAD", check=False)
    sha = r.stdout.strip()
    return sha if r.returncode == 0 and sha else task.base_sha


def diff_files(path: str, base: str) -> list[str]:
    r = workspace.git(path, "diff", "--name-only", f"{base}..HEAD")
    return [x for x in r.stdout.splitlines() if x.strip()]


def diff_text(path: str, base: str, limit: int | None = None) -> str:
    return rev_diff_text(path, f"{base}..HEAD", limit=limit)


def rev_diff_text(path: str, *revs: str, limit: int | None = None, exclude: Sequence[str] = ()) -> str:
    """The diff of any commits — the base..HEAD of a code task, the input of a review task."""
    lim = DIFF_LIMIT if limit is None else limit
    args = ["diff", *revs]
    if exclude:
        args += ["--", ".", *[f":(top,exclude){pat}" for pat in exclude]]
    r = workspace.git(path, *args, check=False)
    out = r.stdout
    return out if len(out) <= lim else out[:lim] + "\n" + _t("gates.diff_cut", size=len(out))


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
    """The test lock stayed busy past the wait (stopped — a stop was requested meanwhile)."""

    def __init__(self, msg: str, *, stopped: bool = False) -> None:
        super().__init__(msg)
        self.stopped = stopped


def with_lock(path: str, fn: Callable[[], object], *, wait_s: float = LOCK_WAIT_S,
              on_wait: Callable[[], None] | None = None, should_stop: Callable[[], bool] | None = None):
    """Run fn under an external flock (shared project test lock). Empty — no lock."""
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
                    raise LockTimeout(_t("gates.lock_busy", path=path, secs=int(wait_s))) from None
                if should_stop is not None and should_stop():
                    raise LockTimeout(_t("gates.lock_stopped"), stopped=True) from None
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
    """(green?, output tail, command). Under the project test resource.

    A busy test lock is a wait, not a red acceptance: LockTimeout propagates and the caller
    waits (the engine re-queues) or refuses (accept) — it never becomes a failing-test fix.
    """
    py = project.python_bin()
    cmd = [py, "-m", "pytest", "-q", *nodes]
    env = scrub_env(dict(os.environ))
    env.update(task_env(task_label, cwd, project.root), PYTHONDONTWRITEBYTECODE="1")
    clear_pycache(cwd)  # stale .pyc (same-size edit within the same second) would give a false green
    lock = ""
    if project.test_resource and project.test_resource in project.resources:
        lock = project.resources[project.test_resource].lock

    def _run():
        try:
            r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, _t("gates.test_timeout", secs=TEST_TIMEOUT_S)
        except OSError as e:
            return False, _t("tasks.pytest_start", err=e)
        tail = "\n".join((r.stdout + "\n" + r.stderr).strip().splitlines()[-TAIL_LINES:])
        return r.returncode == 0, tail

    ok, tail = with_lock(lock, _run, on_wait=on_wait, should_stop=should_stop)
    return ok, tail, " ".join(cmd[2:])


def check(project: ProjectConfig, task: Task, *, run_tests: bool = True, orch_edit: bool = False,
          on_wait: Callable[[], None] | None = None,
          should_stop: Callable[[], bool] | None = None) -> GateResult:
    path = task.worktree
    base = effective_base(project, task)
    head = workspace.head(path)
    g = GateResult(base=base, head=head)
    if workspace.commits_since(path, base) == 0:
        g.repairable.append(problem("no_commit"))
    dirty = workspace.changed_files(path)
    if dirty:
        g.repairable.append(problem("dirty", files=", ".join(dirty[:10])))
    g.diff_files = diff_files(path, base) if base else []
    stat = workspace.git(path, "diff", "--shortstat", f"{base}..HEAD", check=False).stdout.strip()
    g.diffstat = stat
    globs = list(task.limits.get("paths") or [])
    outside = [f for f in g.diff_files if not allowed(f, globs)]
    if outside:
        g.fatal.append(problem("outside", files=", ".join(outside[:10])))
    if not orch_edit:
        res_path = Path(path) / workspace.AHUB_DIR / "result.json"
        try:
            raw = res_path.read_text(encoding="utf-8")
        except OSError:
            g.repairable.append(problem("no_result"))
            res: dict = {}
        else:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                g.repairable.append(problem("result_json", err=str(e)[:200]))
                res = {}
            else:
                if not isinstance(data, dict):
                    g.repairable.append(problem("result_json", err="not a JSON object"))
                    res = {}
                elif not data:
                    g.repairable.append(problem("no_result"))
                    res = {}
                else:
                    res = data
            if res:
                if str(res.get("commit", ""))[:7] != head[:7] or not str(res.get("commit", "")).strip():
                    g.repairable.append(problem("result_commit", got=str(res.get("commit", ""))[:10] or "—",
                                                head=head[:10]))
                files = res.get("files") or []
                extra = [f for f in files if f not in g.diff_files]
                if extra:
                    g.repairable.append(problem("result_files", files=", ".join(map(str, extra[:10]))))
    if run_tests and task.kind is Kind.CODE and not g.repairable and not g.fatal:
        nodes = list(task.limits.get("accept") or [])
        if nodes:
            g.tests_ok, g.tests_tail, g.tests_cmd = run_acceptance(project, path, nodes, task_label=task.label,
                                                                   on_wait=on_wait, should_stop=should_stop)
    return g
