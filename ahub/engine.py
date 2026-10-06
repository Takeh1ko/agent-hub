"""Task engine: the owner drives a task from queue to "Done / Needs decision / Error / Stopped".

One engine instance = one task process (ahub.worker). It:
- takes the task lease and renews it in the background; lease lost — stops at once without writing state;
- tracks phases and records them in the store (the owner is the only writer of an active task);
- runs provider sessions via the shared runner and decides what to do with each step result (architecture §6.3):
  network failure → retry with pause (same session when known); silence → one same-session nudge, then
  "Needs decision"; a message from the orchestrator (ahub nudge) → the worker turn is interrupted and the same
  session continues with it (gates and reviewers are not interrupted by a message — they carry the result of
  the turn); quota/timeout → "Needs decision"; no access/model error/crash → "Error"; requested stop → "Stopped".
V08: scout. Code/routine: gates, panel, merge — M3. Review: the panel over the given input, the findings are
the result (nothing is merged).
Robustness of a live code reload: the owner poll (stop/budget/request) reads the task row through the dataclass
of this code. Old code on a newer schema cannot — POLL_FAIL_MAX failures in a row give the process up: the
provider session is stopped, the task stays active, and the worker exits non-zero (the service re-picks it
as an orphan, on the current code). Never a busy loop.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import socket
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahub import (
    gates,
    prepare,
    prompts,
    providers,
    reasons,
    registry,
    review,
    selfupdate,
    transcript,
    transitions,
    workspace,
)
from ahub import log as hublog
from ahub.config import ProjectConfig
from ahub.i18n import plural
from ahub.i18n import t as _t
from ahub.model import ACTIVE, Ev, Kind, Phase, Role, State
from ahub.providers.base import Act, Activity, Outcome, RunResult, RunSpec
from ahub.providers.runner import PollFailed
from ahub.providers.runner import run as run_session
from ahub.store import Store, Task, is_lock_error
from ahub.time import fmt_local, now_ms

LEASE_MS = 90_000
STOP_POLL_S = 3.0
NUDGE_MAX = 8  # messages from the orchestrator in a row per run (each one is a whole turn)
BUDGET_POLL_S = 30.0
POLL_FAIL_MAX = 3  # owner poll failures in a row — the process gives up (a live reload broke its schema)
REPORT_MAX_BYTES = 18_000  # 12 KB per contract plus margin; over that is flagged, not rejected

_WRITE_TOOLS = {"edit", "write", "patch", "multiedit", "apply_patch", "file_change",
                "write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file"}
_READ_TOOLS = {"read", "grep", "glob", "list", "webfetch", "websearch",
               "view_file", "list_dir", "grep_search", "find_by_name"}
_CMD_TOOLS = {"bash", "run_command", "command_execution"}  # opencode bash; agy run_command; codex command_execution
_CMD_KEYS = {"command", "commandline", "cmd"}  # agy keeps the command in the parameter CommandLine


def _tool_command(data: dict) -> str:
    """Command line of a tool call: opencode `command`, agy `CommandLine` inside tool parameters,
    codex `command` (`/bin/bash -lc '…'`)."""
    inp = data.get("input") if isinstance(data, dict) else None
    if not isinstance(inp, dict):
        return ""
    for key, val in inp.items():
        if isinstance(key, str) and key.lower() in _CMD_KEYS and isinstance(val, str):
            return val[:200]
    return ""


class LeaseLost(RuntimeError):
    """Lease taken away: stop work, leave state alone."""


class ReviewInputError(RuntimeError):
    """The --input of a review task names nothing that exists — the task needs a decision, not a worker."""


def _tb_has_hub(e: BaseException) -> bool:
    """The traceback goes through hub code (ahub.*) — not a provider subprocess output."""
    tb = e.__traceback__
    while tb is not None:
        fn = (tb.tb_frame.f_code.co_filename or "").replace("\\", "/")
        if "/ahub/" in fn or fn.endswith("/ahub.py"):
            return True
        mod = tb.tb_frame.f_globals.get("__name__", "")
        if isinstance(mod, str) and (mod == "ahub" or mod.startswith("ahub.")):
            return True
        tb = tb.tb_next
    return False


def _stale_code_error(e: BaseException) -> bool:
    """Old worker on new code: a hub import broke or a hub attribute vanished.

    Only hub code counts — an ImportError in a provider subprocess output never raises here,
    it arrives as Outcome text, not as an exception.
    """
    if isinstance(e, ImportError):
        name = getattr(e, "name", "") or ""
        if isinstance(name, str) and name.startswith("ahub"):
            return True
        if "ahub" in str(e):
            return True
        return _tb_has_hub(e)
    if isinstance(e, AttributeError):
        # a hub module lost an attribute under a live update: the message names it
        if "ahub" in str(e):
            return True
        return False
    return False


def _code_fingerprint() -> str:
    """Best-effort fingerprint of the hub code ('' when unknown)."""
    try:
        return selfupdate.code_fingerprint()
    except Exception:
        return ""


def _code_changed_since(fp: str) -> bool:
    """Hub code changed since fp was taken; False when either fingerprint is unknown.

    Unknown never counts as changed: a genuine bug must go to error, not loop re-picks on the
    same code.
    """
    if not fp:
        return False
    now = _code_fingerprint()
    return bool(now) and now != fp


UNFIXABLE_SCOUT = ("scout_files", "scout_commits")  # a scout that touched files — a repair prompt cannot fix it

_SHA = re.compile(r"[0-9a-fA-F]{7,40}")
FILE_LIMIT = 60_000  # one file of a "review these files" input


def _ref(worktree: str, spec: str) -> str:
    """The commit the name (a branch, a tag, a sha) resolves to ('' — nothing by that name)."""
    r = workspace.git(worktree, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}", check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


def _is_secret(name: str, project: ProjectConfig) -> bool:
    p = Path(name)
    return any(fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(p.name, pat) for pat in project.secret_excludes)


def _file_in_repo(worktree: str, project: ProjectConfig, name: str) -> bool:
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        return False
    if (Path(worktree) / p).is_file():
        return True
    if (Path(project.root) / p).is_file():
        return True
    r = workspace.git(worktree, "ls-files", name, check=False)
    return bool(r.stdout.strip())


def _file_of_copy(worktree: str, name: str) -> bool:
    """The name is a file of the copy — a review never reads outside its worktree."""
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        return False
    full = Path(worktree) / p
    if not full.is_file():
        return False
    try:
        resolved = full.resolve(strict=True)
        resolved.relative_to(Path(worktree).resolve(strict=True))
    except (ValueError, OSError):
        return False
    return True


def review_material(project: ProjectConfig, worktree: str, spec: str) -> str:
    """What the panel reads for a review task: the diff of a branch/commit/range, or the content of files.

    A branch is read from its merge-base with the work branch, a commit as `sha^1 sha` for a merge or
    against the empty tree for a root commit, an `a..b` range as it is. Files are read as they are in the
    copy — there is no diff of them. Everything is capped like the diff of a code task, and secret excludes
    are filtered out.
    """
    spec = (spec or "").strip()
    if ".." in spec:
        left, right = spec.split("..", 1)
        if not (_ref(worktree, left) and _ref(worktree, right)):
            raise ReviewInputError(_t("engine.review_input_bad", input=spec))
        diff = gates.rev_diff_text(worktree, spec, exclude=project.secret_excludes)
        if not diff.strip():
            raise ReviewInputError(_t("engine.review_empty", input=spec))
        return diff
    names = [n for n in re.split(r"[,\s]+", spec) if n]
    if names:
        secret_names = [n for n in names if _is_secret(n, project) and _file_in_repo(worktree, project, n)]
        valid_names = [n for n in names if _file_of_copy(worktree, n) and not _is_secret(n, project)]
        if valid_names and len(valid_names) + len(secret_names) == len(names):
            files_text = [_file_text(worktree, n) for n in valid_names]
            text = "\n\n".join(t for t in files_text if t.strip())
            if not text.strip():
                raise ReviewInputError(_t("engine.review_empty", input=spec))
            if len(text) > gates.DIFF_LIMIT:
                text = text[:gates.DIFF_LIMIT] + "\n" + _t("engine.review_cut_total", size=len(text))
            return text
        if secret_names and len(secret_names) == len(names):
            raise ReviewInputError(_t("engine.review_secret_excluded", file=secret_names[0]))
    sha = _ref(worktree, spec)
    if sha:
        if _SHA.fullmatch(spec):  # a commit — what it itself changed
            parents = workspace.git(worktree, "rev-list", "--parents", "-n1", sha, check=False).stdout.split()
            if len(parents) <= 1:  # root commit — diff against the empty tree
                empty_tree = workspace.git(worktree, "hash-object", "-t", "tree", "/dev/null",
                                           check=False).stdout.strip() or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
                diff = gates.rev_diff_text(worktree, empty_tree, sha, exclude=project.secret_excludes)
            elif len(parents) > 2:  # merge commit — diff against first parent
                diff = gates.rev_diff_text(worktree, f"{sha}^1", sha, exclude=project.secret_excludes)
            else:
                diff = gates.rev_diff_text(worktree, f"{parents[1]}..{sha}", exclude=project.secret_excludes)
            if not diff.strip():
                raise ReviewInputError(_t("engine.review_empty", input=spec))
            return diff
        base = workspace.git(worktree, "merge-base", project.work_branch, sha, check=False).stdout.strip()
        if not base:  # unrelated histories — nothing to count the branch from
            raise ReviewInputError(_t("engine.review_input_bad", input=spec))
        diff = gates.rev_diff_text(worktree, f"{base}..{sha}", exclude=project.secret_excludes)
        if not diff.strip():
            raise ReviewInputError(_t("engine.review_empty", input=spec))
        return diff
    raise ReviewInputError(_t("engine.review_input_bad", input=spec))


def _file_text(worktree: str, name: str) -> str:
    """One file of a review input: its current content under its path."""
    body = (Path(worktree) / name).read_text(encoding="utf-8", errors="replace")
    if len(body) > FILE_LIMIT:
        body = body[:FILE_LIMIT] + "\n" + _t("engine.review_cut", size=len(body))
    return f"### {name}\n```\n{body}\n```"


def _findings_summary(findings: list[review.Finding]) -> str:
    """One line for the state and the result: '3 findings: 1 high, 2 medium' / 'no findings'."""
    if not findings:
        return _t("engine.findings_none")
    counts = {s: sum(1 for f in findings if f.severity == s) for s in ("high", "medium", "low")}
    parts = ", ".join(f"{n} {s}" for s, n in counts.items() if n)
    return plural(len(findings), "engine.findings_one", "engine.findings_few", "engine.findings_many",
                  parts=parts)


def _gate_problems(g: gates.GateResult) -> list[dict]:
    """The gate problems as sub-reasons — to store on the task, not translated text."""
    items = gates.codes(g.repairable)
    if g.tests_ok is False:
        items.append(reasons.part("gate_accept_red"))
    return items


def owner_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class Settled:
    """How an engine step ended (for tests and logs). `reason` — the text, in the current language."""

    state: State
    reason: str = ""
    busy: bool = False


class Engine:
    def __init__(self, store: Store, project: ProjectConfig, task_id: int, *, owner: str | None = None,
                 sleep: Callable[[float], None] = time.sleep, lease_ms: int = LEASE_MS) -> None:
        self.store = store
        self.project = project
        self.task_id = int(task_id)
        self.owner = owner or owner_token()
        self.sleep = sleep
        self.lease_ms = lease_ms
        self.lost = threading.Event()
        self._stop_cache: tuple[float, bool] = (0.0, False)
        self._poll_fails = 0
        self._phase: str = ""
        self._deadline_ms: int | None = None
        self.budget_hit = False
        self._budget_at = 0.0
        self._soft_sent = False
        self._nudge_turns = 0
        self._saw_tools = False
        self._code0 = _code_fingerprint()  # hub code as this worker started; stale imports compare against it
        self.log = hublog.get("engine", task=self.task_id, project=project.name)

    def run(self) -> Settled:
        if not transitions.acquire(self.store, self.task_id, self.owner, pid=os.getpid(), lease_ms=self.lease_ms):
            self.log.info("task owned by another owner — exiting")
            t = self.store.get_task(self.task_id)
            return Settled(t.state if t else State.ERROR, reasons.text(reasons.dump("busy")), busy=True)
        try:
            from ahub import loops as _loops

            _loops.record_pick(self.store, self.task_id)
        except Exception:
            self.log.exception("pick not counted")
        try:
            with transitions.keep_lease(self.store, self.task_id, self.owner, lease_ms=self.lease_ms,
                                        on_lost=self.lost.set, name=f"engine-lease-T{self.task_id}"):
                return self._run()
        except LeaseLost:
            t = self.store.get_task(self.task_id)
            return Settled(t.state if t else State.ERROR, reasons.text(reasons.dump("lease_lost")))
        except PollFailed:
            raise  # the task stays active; the worker exits non-zero and the service re-picks it
        except workspace.WorkspaceError as e:
            self.log.error("worktree: %s", e)
            return self._settle(State.ERROR, reasons.dump("prepare_failed", err=e))
        except Exception as e:
            if is_lock_error(e):
                # Lock contention that survived the store retries: transient, same session.
                self.log.warning("hub database is busy — back in the queue: %s", e)
                try:
                    return self._settle(State.QUEUED, reasons.dump("hub_locked"))
                except Exception:
                    self.log.exception("failed to requeue on lock")
                    raise
            if _stale_code_error(e) and _code_changed_since(self._code0):
                # the hub code changed under this worker: like the poll failure path — one log line,
                # the provider group is stopped, the worker exits 4 and the service re-picks the task.
                # Unchanged code is a genuine bug and settles error as before (no re-pick loop).
                self.log.error("stale hub code (%s: %s) — the task is left to the service",
                               type(e).__name__, str(e)[:200])
                try:
                    from ahub.providers import runner as _runner

                    _runner.request_stop()
                except Exception:
                    self.log.exception("stopping the provider failed")
                raise PollFailed(f"stale code: {type(e).__name__}: {e}") from e
            self.log.exception("engine crashed")
            try:
                # an exception message is technical detail — stored as text, not as a reason code
                return self._settle(State.ERROR, f"hub failure: {type(e).__name__}: {e}")
            except Exception:
                self.log.exception("failed to record error")
                raise
        finally:
            try:
                transitions.release(self.store, self.task_id, self.owner)
            except sqlite3.Error:
                self.log.exception("release failed")

    # --- helpers ---

    def task(self) -> Task:
        t = self.store.get_task(self.task_id)
        if t is None:
            raise RuntimeError(_t("trans.no_task", id=self.task_id))
        return t

    def _check_lease(self) -> None:
        if self.lost.is_set():
            raise LeaseLost()

    def move(self, to: State, reason: str = "", **kw) -> Task:
        self._check_lease()
        return transitions.move(self.store, self.task_id, to, reason=reason, by="engine", owner=self.owner, **kw)

    def _settle(self, to: State, reason: str = "", payload: dict | None = None) -> Settled:
        """Finish the task in `to` with this reason (a code blob or free text). Returns what a reader sees."""
        t = self.task()
        if t.state is to:
            return Settled(to, reasons.text(reason))
        if t.state not in ACTIVE and t.state is not State.QUEUED:
            return Settled(t.state, reasons.text(t.state_reason))  # already decided (e.g. stopped)
        stored = reason if reasons.load(reason) else reason[:500]  # free text is clipped, a code is not
        if to is State.QUEUED:
            looped = self._loop_guard(stored)
            if looped is not None:
                to, stored = looped
        cost = self.task_cost()
        body = {"cost_go": round(cost[0], 4), "cost_usd": round(cost[1], 4)}
        body.update(payload or {})
        self.move(to, stored, payload=body)
        self.log.info("settled: %s%s", to.value, f" ({reasons.text(stored)[:200]})" if stored else "")
        lim = self.task().limits
        if lim.get("orphans"):  # episode done — orphan counter restarts
            lim = dict(lim)
            lim.pop("orphans", None)
            self.store.update_task(self.task_id, limits=lim)
        from ahub import archive

        archive.write_task(self.store, self.project, self.task_id)
        return Settled(to, reasons.text(stored))

    def _loop_guard(self, stored: str) -> tuple[State, str] | None:
        """Same QUEUED reason N times without progress → needs_decision `loop`, else None.

        Progress is a new commit, a new review verdict or a forward round; anything
        else grows the streak in limits["loop"]. The loop reason carries the evidence.
        """
        from ahub import loops as _loops

        code = _loops.reason_code(stored)
        if not code or code == _loops.LOOP_CODE:
            return None
        t = self.task()
        try:
            loop_n, _, _ = _loops.limits_of()
        except Exception:
            loop_n = 3
        try:
            head = _loops.task_head(t)
            verdicts = _loops.verdict_count(t)
            loop = _loops.note_settle(self.store, t, code, head=head, verdicts=verdicts)
        except Exception:
            self.log.exception("loop not recorded")
            return None
        n = int(loop.get("n") or 0)
        if n < loop_n:
            return None
        self.log.warning("loop T%d: %s %d× without progress — needs decision", self.task_id, code, n)
        return State.NEEDS_DECISION, _loops.loop_reason(code, n, int(loop.get("first") or 0))

    def _stuck_guard(self, kind: str, sid: str | None, r) -> Settled | None:
        """K continue turns with no commit and no tool activity → needs_decision `stuck_session`.

        Only `continue` turns grow the streak; any other kind restarts it. Returns the
        stuck settle, or None to continue normally.
        """
        from ahub import loops as _loops

        try:
            _, stuck_n, _ = _loops.limits_of()
        except Exception:
            stuck_n = 3
        t = self.task()
        try:
            head = _loops.task_head(t)
        except Exception:
            head = ""
        sess = sid or (r.session_id or "")
        try:
            if kind != "continue":
                _loops.clear_stuck_progress(self.store, t, head=head, session=sess)
                return None
            stuck = _loops.note_continue(self.store, t, head, sess, bool(self._saw_tools))
        except Exception:
            self.log.exception("stuck not recorded")
            return None
        n = int(stuck.get("n") or 0)
        if n < stuck_n:
            return None
        self.log.warning("stuck session T%d: %d continues without progress — needs decision",
                         self.task_id, n)
        outcome = getattr(r, "outcome", "")
        try:
            outcome_s = outcome.value if hasattr(outcome, "value") else str(outcome or "")
        except Exception:
            outcome_s = ""
        return self._settle(State.NEEDS_DECISION,
                            _loops.stuck_reason(n, outcome=outcome_s, session=sess))

    def stop_requested(self) -> bool:
        """The turn must stop: the lease is lost, the budget is spent, a stop was requested.

        A nudge is NOT here: it is a message, not a reason to kill what is running — it is delivered in
        the next worker turn (`_step_with_continue`). So the gates and the reviewer sessions, which
        carry the result of the turn, wait for the message instead of being cut short by it.
        """
        if self.lost.is_set() or self.budget_hit:
            return True
        if self._poll_request() == "stop":
            return True
        now = time.monotonic()
        if now - self._budget_at >= BUDGET_POLL_S:
            self._budget_at = now
            if self.over_budget(live=True):
                self.budget_hit = True
                return True
        return False

    def interrupt_requested(self) -> bool:
        """A worker turn is interrupted by a stop and by a nudge: the text goes into the next turn."""
        return self.stop_requested() or self._poll_request() == "nudge"

    def _poll(self, what: str, read: Callable[[], Any], default: Any) -> tuple[bool, Any]:
        """(read ok, value) of one owner poll (the task row, the budget).

        A couple of failures are tolerated and the default is returned; POLL_FAIL_MAX in a row mean this
        code cannot read its own schema (a live reload added columns): log once and raise PollFailed — the
        caller stops the provider session and the process gives the task back to the service.
        """
        try:
            val = read()
        except Exception as e:
            self._poll_fails += 1
            if self._poll_fails < POLL_FAIL_MAX:
                self.log.warning("%s poll failed (%d/%d): %s", what, self._poll_fails, POLL_FAIL_MAX, e)
                return False, default
            self.log.error("%s poll failed %d times in a row (%s: %s) — the task is left to the service",
                           what, self._poll_fails, type(e).__name__, str(e)[:200])
            raise PollFailed(f"{what}: {type(e).__name__}: {e}") from e
        self._poll_fails = 0
        return True, val

    def _poll_request(self) -> str:
        """Owner request from the task row ('stop' | 'nudge' | ''), at most once in STOP_POLL_S."""
        now = time.monotonic()
        at, val = self._stop_cache
        if now - at < STOP_POLL_S:
            return val
        ok, fresh = self._poll("request", lambda: self.task().request, "")
        if ok:  # a failed read is not cached — the next poll tries again at once
            self._stop_cache = (now, fresh)
        return fresh

    def pending_nudge(self) -> str:
        """Text of an undelivered nudge ('' — nothing to deliver)."""
        def _read() -> str:
            t = self.task()
            return t.request_text if t.request == "nudge" else ""

        _, text = self._poll("nudge", _read, "")
        return text

    def _take_request_back(self, kind: str, text: str = "") -> None:
        """The owner took its request from the row: the poll cache must not serve it to the turn it was for."""
        transitions.clear_request(self.store, self.task_id, kind=kind, text=text)
        self._stop_cache = (time.monotonic(), "")

    def over_budget(self, *, live: bool = False) -> bool:
        """Task budget (whole task incl. review): 80% — journal event; 100% — True."""
        _, over = self._poll("budget", lambda: self._over_budget(live), False)
        return over

    def _over_budget(self, live: bool) -> bool:
        t = self.task()
        go, usd = self.task_cost()
        if live:  # all running task sessions (reviewers run in parallel): provider usage minus recorded
            for s in self.store.list_sessions(self.task_id, status="running"):
                if not s.external_id:
                    continue
                try:
                    u = providers.get(s.provider).usage(s.external_id)
                except Exception:
                    u = None
                if u is not None:
                    go += max(0.0, (u.cost_go or 0.0) - (s.cost_go or 0.0))
                    usd += max(0.0, (u.cost_usd or 0.0) - (s.cost_usd or 0.0))
        over = (t.budget_go > 0 and go >= t.budget_go) or (usd > t.budget_usd)
        if not over and not self._soft_sent and t.budget_go > 0 and go >= 0.8 * t.budget_go:
            self._soft_sent = True
            self.store.add_event("budget_soft", task_id=self.task_id, project=self.project.name,
                                 payload={"go": round(go, 4), "budget_go": t.budget_go})
        return over

    def _pause(self, secs: float, should_stop: Callable[[], bool] | None = None) -> bool:
        """Interruptible pause. False — a stop was requested while waiting."""
        stop = should_stop or self.interrupt_requested
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if stop():
                return False
            self.sleep(min(1.0, max(0.0, end - time.monotonic())))
        return True

    def set_phase(self, phase: Phase) -> None:
        if phase.value != self._phase:
            self._phase = phase.value
            try:
                self.store.update_task(self.task_id, phase=phase.value)
            except sqlite3.Error:
                self.log.exception("phase not recorded")

    def _on_activity(self, act: Activity) -> None:
        if act.kind in (Act.TOOL_START, Act.TOOL_END):
            self._saw_tools = True
            tool = act.tool.lower()
            if tool in _WRITE_TOOLS:
                self.set_phase(Phase.WRITING)
            elif tool in _CMD_TOOLS:
                self.set_phase(Phase.TESTING if "pytest" in _tool_command(act.data) else Phase.STUDYING)
            elif tool in _READ_TOOLS:
                self.set_phase(Phase.STUDYING)

    def task_cost(self) -> tuple[float, float]:
        go = usd = 0.0
        for s in self.store.list_sessions(self.task_id):
            go += s.cost_go or 0.0
            usd += s.cost_usd or 0.0
        return go, usd

    def _remaining_s(self) -> int:
        if self._deadline_ms is None:
            return 90 * 60
        return max(1, int((self._deadline_ms - now_ms()) / 1000))

    def _session_row(self, provider: str, role: Role, alias: str, round_no: int, session_id: str | None,
                     log_path: str, prompts_summary: str = "", effort: str = "") -> int:
        """Session row: resume — same row (provider id is unique), new — new row."""
        if session_id:
            for s in self.store.list_sessions(self.task_id):
                if s.provider == provider and s.external_id == session_id:
                    self.store.update_session(s.id, status="running", outcome="", ended_at=None, log_path=log_path)
                    return s.id
        return self.store.add_session(task_id=self.task_id, provider=provider, role=role.value, model=alias,
                                       round=round_no, external_id=session_id or "", log_path=log_path,
                                       prompts=prompts_summary, effort=effort)

    def _close_session(self, row: int) -> None:
        """The provider group is gone and the turn is lost — the row must not stay 'running'."""
        try:
            self.store.update_session(row, status="killed", outcome=Outcome.KILLED.value, ended_at=now_ms())
        except sqlite3.Error:
            self.log.exception("session %d not closed", row)

    def _note_prompt(self, log_path: str, kind: str, prompt: str) -> None:
        """The prompt of the turn into the sidecar next to the log — `ahub follow` reads it.

        The kinds are transcript.PROMPT_KINDS: start | continue | repair | rework | stop | review | nudge.
        """
        try:
            path = transcript.prompts_path(log_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                turn = path.read_text(encoding="utf-8", errors="replace").count("\n") + 1
            except FileNotFoundError:
                turn = 1
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now_ms(), "turn": turn, "kind": kind, "text": prompt},
                                   ensure_ascii=False) + "\n")
        except OSError as e:
            self.log.warning("prompt of the turn not saved: %s", e)

    def session(self, role: Role, alias: str, prompt: str, *, session_id: str | None = None,
                keep_session_on_retry: bool = True, log_name: str = "", schema: dict | None = None,
                cwd: str | None = None, prompt_kind: str = "start",
                stop_predicate: Callable[[], bool] | None = None, prompts_summary: str = "") -> RunResult:
        """One worker step with retries on network failure (architecture §6.3).

        `prompt_kind` says what the prompt is (start | continue | repair | rework | stop | review | nudge) and goes
        into the prompts sidecar, which is what `ahub follow` shows as the turn header. One call is one
        turn: a network retry repeats the run of the same prompt, not the turn.

        `prompts_summary` — the canonical prompt-layers summary, stored on a new session row (reviewers).

        `stop_predicate` — what interrupts this turn. By default a nudge does (a worker turn carries the
        message); a reviewer or a save-and-stop turn passes `self.stop_requested` and waits for its turn.

        alias may be ALIAS[:EFFORT] (legacy mapped); the session row keeps the base alias + stored effort.
        """
        base = registry.base_alias(alias)
        stored = registry.stored_effort(alias)
        entry = registry.get(self.store, alias)
        prov = providers.get(entry.provider)
        t = self.task()
        cwd = cwd or t.worktree
        tmo = self.project.timeouts
        self._check_lease()
        self._quota_clear()  # provider work starts: a quota-hold episode is over
        should_stop = stop_predicate or self.interrupt_requested
        log_path = str(Path(cwd) / workspace.AHUB_DIR / "logs" / f"{log_name or role.value}.log")
        self._note_prompt(log_path, prompt_kind, prompt)
        attempt = 0
        while True:
            self._check_lease()
            row = self._session_row(prov.name, role, base, t.round, session_id, log_path, prompts_summary,
                                    stored)

            def on_session(sid: str, _row=row) -> None:
                try:
                    self.store.update_session(_row, external_id=sid)
                except sqlite3.IntegrityError:
                    self.log.warning("session %s already linked to another row", sid)

            spec = RunSpec(prompt=prompt, cwd=cwd, model_id=entry.model_id, variant=entry.variant,
                           session_id=session_id, log_path=log_path, timeout_s=self._remaining_s(),
                           idle_s=tmo.idle_s, schema=schema)
            try:
                r = run_session(prov, spec, on_activity=self._on_activity, on_session=on_session,
                                on_start=lambda pid, _row=row: self.store.update_session(_row, pid=pid),
                                should_stop=should_stop)
            except PollFailed:  # the runner killed the provider group — the row must not stay running
                self._close_session(row)
                raise
            except (ImportError, AttributeError) as e:
                if not _stale_code_error(e) or not _code_changed_since(self._code0):
                    raise
                self._close_session(row)
                self.log.error("stale hub code (%s: %s) — the task is left to the service",
                               type(e).__name__, str(e)[:200])
                raise PollFailed(f"stale code: {type(e).__name__}: {e}") from e
            u = r.usage
            fields: dict = {"status": "ok" if r.ok else ("killed" if r.outcome is Outcome.KILLED else "failed"),
                            "outcome": r.outcome.value, "ended_at": r.ended_ms}
            if u is not None:
                fields.update(cost_go=u.cost_go or 0.0, cost_usd=u.cost_usd or 0.0,
                              tokens={k: v for k, v in vars(u).items() if v is not None})
            if r.session_id:
                fields["external_id"] = r.session_id
            try:
                self.store.update_session(row, **fields)
            except sqlite3.IntegrityError:
                fields.pop("external_id", None)
                self.store.update_session(row, **fields)
            if r.outcome is Outcome.TRANSIENT and attempt < tmo.retry_max:
                attempt += 1
                pause = tmo.retry_pause_s * (2 ** (attempt - 1))
                self.store.add_event("retry", task_id=self.task_id, project=self.project.name,
                                     payload={"reason": r.error[:200], "attempt": attempt, "of": tmo.retry_max,
                                              "pause_s": pause, "text": _t("engine.retry_text", attempt=attempt,
                                                                            of=tmo.retry_max,
                                                                            pause=int(pause))})
                self.log.warning("provider failure: %s → retry %d/%d in %d s", r.error[:200], attempt,
                                 tmo.retry_max, pause)
                self.set_phase(Phase.WAITING)
                if not self._pause(pause, self.stop_requested):
                    return RunResult(Outcome.KILLED, r.session_id, error=_t("engine.pause_killed"))
                if keep_session_on_retry and r.session_id:
                    session_id = r.session_id
                continue
            return r

    # --- task step ---

    def _run(self) -> Settled:
        t = self.task()
        if t.state is State.QUEUED:
            t = self.move(State.PREPARING, reasons.dump("taken"))
        elif t.state not in ACTIVE:
            return Settled(t.state, reasons.text(reasons.dump("not_active")))
        limit_min = int(t.limits.get("time_limit_min") or 60)
        self._deadline_ms = now_ms() + limit_min * 60_000
        if self.over_budget():
            return self._settle(State.NEEDS_DECISION, reasons.dump("budget_before"))
        if t.kind is Kind.SCOUT:
            return self._scout(t)
        if t.kind is Kind.REVIEW:
            return self._review(t)
        if t.kind in (Kind.CODE, Kind.ROUTINE):
            return self._code(t)
        return self._settle(State.NEEDS_DECISION, reasons.dump("unsupported_kind", kind=t.kind.value))

    def _prepare(self, t: Task) -> Task:
        if t.state is State.PREPARING:
            ws = workspace.ensure(self.project, t.id)
            prepare.hide_secrets(self.project, ws.path)
            fields = {"worktree": ws.path, "branch": ws.branch}
            if not t.base_sha:
                fields["base_sha"] = ws.base_sha
            t = self.move(State.WORKING, reasons.dump("worker_started"), fields={**fields, "round": max(1, t.round)})
        return t

    def _outcome_to_state(self, r: RunResult, *, role: Role = Role.EXECUTOR,
                          model_alias: str = "") -> tuple[State, str] | None:
        """Step result after which there is nothing to continue. None — step ok or handled separately."""
        if r.outcome is Outcome.KILLED:
            if self.lost.is_set():
                raise LeaseLost()
            if self.budget_hit:
                return State.NEEDS_DECISION, reasons.dump("budget_gone")
            return State.STOPPED, reasons.dump("stopped")
        if r.outcome is Outcome.TIMEOUT:
            return State.NEEDS_DECISION, reasons.dump("time_limit", err=r.error)
        if r.outcome is Outcome.QUOTA:
            return self._handle_quota_outcome(r, role=role, model_alias=model_alias)
        if r.outcome is Outcome.TRANSIENT:
            return State.NEEDS_DECISION, reasons.dump("transient_out", err=r.error[:300])
        if r.outcome in (Outcome.NO_ACCESS, Outcome.MODEL_ERROR, Outcome.CRASH, Outcome.NOT_STARTED):
            return State.ERROR, reasons.dump("step_failed", outcome=r.outcome.value, err=r.error[:400])
        return None

    def _exec_ref(self, t=None) -> str:
        """Executor ref ALIAS[:EFFORT] for a run (legacy executors map to base + effort)."""
        task = t if t is not None else self.task()
        base = registry.base_alias(task.executor or "")
        stored = getattr(task, "effort", "") or registry.stored_effort(task.executor or "")
        return registry.model_ref(base, stored)

    def _panel_refs(self, t=None) -> list[str]:
        """Review panel refs ALIAS[:EFFORT] (parallel efforts when stored)."""
        task = t if t is not None else self.task()
        models = list(task.review.get("models") or [])
        efforts = list(task.review.get("efforts") or [])
        out: list[str] = []
        for i, m in enumerate(models):
            base = registry.base_alias(str(m))
            stored = str(efforts[i]) if i < len(efforts) else registry.stored_effort(str(m))
            out.append(registry.model_ref(base, stored))
        return out

    def _handle_quota_outcome(self, r: RunResult, *, role: Role = Role.EXECUTOR,
                              model_alias: str = "") -> tuple[State, str]:
        from ahub import config, quota

        alias = model_alias or self._exec_ref()
        _prov, buckets = quota.get_model_buckets(self.store, alias, force=True)

        hub_cfg = config.load_hub()
        quota_cfg = hub_cfg.quota
        fb_role = quota_cfg.fallback_reviewer if role is Role.REVIEWER else quota_cfg.fallback_executor
        fallback = fb_role or quota_cfg.fallback

        t = self.task()
        reason, event_text = quota.describe_error(t.label, buckets, r.error, fallback)
        if fallback:
            if role is Role.REVIEWER:
                # a reviewer never takes the executor's seat: only its panel entry moves
                old_model = alias
                quota.swap_panel_ref(self.store, t, alias, fallback)
                quota.expire_hold(self.store, t)  # wake at once; the resume info stays
            else:
                old_model = quota.swap_executor(self.store, t, fallback)
                self._quota_clear()  # a fresh model does the work from scratch
            self.store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                                 payload={"from": old_model, "to": fallback, "text": event_text})
        elif role is not Role.REVIEWER:
            # the session that hit the quota error is poisoned (agy answers a resume with the old
            # error again) — abandon it; the work is on the branch, the new session starts fresh
            self.store.update_task(t.id, limits={**t.limits, "fresh_session": True})
            self._quota_hold(quota.HOLD_STAGE_EXECUTOR, reason, quota.pick_window(buckets), err=r.error)
        else:
            self._quota_hold(quota.HOLD_STAGE_REVIEW, reason, quota.pick_window(buckets), err=r.error)
        return State.QUEUED, reason

    def _step_with_continue(self, role: Role, alias: str, prompt: str, *, session_id: str | None,
                            log_name: str, prompt_kind: str = "start") -> tuple[RunResult, tuple[State, str] | None]:
        """Worker step; silence → one same-session nudge; a nudge from the orchestrator → the same session."""
        r = self.session(role, alias, prompt, session_id=session_id, log_name=log_name, prompt_kind=prompt_kind)
        r = self._take_nudge(role, alias, r, session_id, log_name)
        if r.outcome is Outcome.SILENCE:
            self.store.add_event("silence", task_id=self.task_id, project=self.project.name,
                                 payload={"secs": r.silence_s, "action": "continue",
                                          "text": _t("engine.silence_text", secs=r.silence_s)})
            r = self.session(role, alias, prompts.CONTINUE_PROMPT, session_id=r.session_id or session_id,
                             log_name=log_name, prompt_kind="continue")
            r = self._take_nudge(role, alias, r, session_id, log_name)
            if r.outcome is Outcome.SILENCE:
                return r, (State.NEEDS_DECISION, reasons.dump("silence_twice", secs=r.silence_s))
        return r, self._outcome_to_state(r, role=role, model_alias=alias)

    def _take_nudge(self, role: Role, alias: str, r: RunResult, session_id: str | None,
                    log_name: str) -> RunResult:
        """Deliver what the orchestrator asked into the same session.

        Round, budget and gates are untouched — this is one more turn. A message that arrives during a
        nudge turn interrupts it (a turn carries a message) and is delivered in the next one: the turns
        of all nudges in one run are capped at NUDGE_MAX.
        """
        while self._nudge_turns < NUDGE_MAX:
            text = self.pending_nudge()
            if not text:
                return r
            self._take_request_back("nudge", text)
            self._nudge_turns += 1
            self.log.info("nudge into the %s session: %s", role.value, text[:200])
            r = self.session(role, alias, prompts.nudge_prompt(text), session_id=r.session_id or session_id,
                             log_name=log_name, prompt_kind="nudge")
            if r.outcome is Outcome.KILLED and not self.stop_requested() and self.pending_nudge():
                continue  # the turn was cut short by the next message — it is delivered right here
            # a turn that ended badly settles the task — the message that arrived meanwhile does not paper over it
            if self._outcome_to_state(r) is not None:
                return r
        return r

    # --- scout ---

    def _scout(self, t: Task) -> Settled:
        t = self._prepare(t)
        held, swapped = self._quota_gate_executor(t)
        if held is not None:
            return held
        if swapped:
            t = self.task()
        self.set_phase(Phase.STUDYING)
        prev = [s for s in self.store.list_sessions(t.id) if s.role == Role.SCOUT.value and s.external_id]
        resume_sid = prev[-1].external_id if prev and not t.limits.get("fresh_session") else None  # resume
        self._clear_fresh(t)
        if resume_sid:
            prompt = prompts.CONTINUE_PROMPT
        else:
            prompt, summary, _ = prompts.scout_prompt(self.project, t)
            self._record_prompts(t, summary)
        self._saw_tools = False
        r, final = self._step_with_continue(Role.SCOUT, self._exec_ref(t), prompt, session_id=resume_sid,
                                            log_name="scout",
                                            prompt_kind="continue" if resume_sid else "start")
        stuck = self._stuck_guard("continue" if resume_sid else "start", r.session_id or resume_sid, r)
        if stuck is not None:
            return stuck
        if final is not None:
            return self._settle(*final)
        problems = self._check_scout(t)
        if problems and not any(p.code in UNFIXABLE_SCOUT for p in problems):
            self._saw_tools = False
            r, final = self._step_with_continue(Role.SCOUT, self._exec_ref(t),
                                                prompts.repair_prompt("; ".join(problems)),
                                                session_id=r.session_id, log_name="scout", prompt_kind="repair")
            stuck = self._stuck_guard("repair", r.session_id, r)
            if stuck is not None:
                return stuck
            if final is not None:
                return self._settle(*final)
            problems = self._check_scout(t)
        if problems:
            return self._settle(State.NEEDS_DECISION,
                                reasons.dump("scout_bad", problems=gates.codes(problems, prefix="")))
        res = self._result(t)
        if res.get("status") == "blocked":
            summary = str(res.get("summary", ""))
            return self._settle(State.NEEDS_DECISION, reasons.dump("blocked", summary=summary),
                                payload={"summary": summary})
        report = Path(t.worktree) / workspace.AHUB_DIR / "report.md"
        try:
            st_size = report.stat().st_size
        except OSError:  # the report is gone between the check and now (a directory is caught by _check_scout)
            return self._settle(State.NEEDS_DECISION,
                                reasons.dump("scout_bad", problems=[reasons.part("no_report")]))
        return self._settle(State.DONE, reasons.dump("report_ready"),
                            payload={"summary": str(res.get("summary", ""))[:500],
                                     "report_bytes": st_size})

    def _result(self, t: Task) -> dict:
        p = Path(t.worktree) / workspace.AHUB_DIR / "result.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _check_scout(self, t: Task) -> list[gates.Problem]:
        """What is wrong with the scout result — empty list when it matches the form."""
        problems: list[gates.Problem] = []
        changed = workspace.changed_files(t.worktree)
        if changed:
            problems.append(gates.problem("scout_files", files=", ".join(changed[:10])))
        if workspace.commits_since(t.worktree, t.base_sha):
            problems.append(gates.problem("scout_commits"))
        base = Path(t.worktree) / workspace.AHUB_DIR
        res_path = base / "result.json"
        if not res_path.exists():
            problems.append(gates.problem("no_result"))
            res = {}
        else:
            try:
                raw = res_path.read_text(encoding="utf-8")
            except OSError:
                problems.append(gates.problem("result_not_json"))
                res = {}
            else:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as e:
                    problems.append(gates.problem("result_json", err=str(e)[:200]))
                    res = {}
                else:
                    if not isinstance(data, dict) or not data:
                        problems.append(gates.problem("result_not_json"))
                        res = {}
                    else:
                        res = data
                        if not str(res.get("summary", "")).strip():
                            problems.append(gates.problem("empty_summary"))
                        if res.get("status") not in ("done", "blocked"):
                            problems.append(gates.problem("bad_status"))
        report = base / "report.md"
        try:
            report_ok = report.is_file() and bool(report.read_text(encoding="utf-8", errors="replace").strip())
        except OSError:
            report_ok = False
        if not report_ok and res.get("status") != "blocked":
            problems.append(gates.problem("no_report"))
        return problems

    # --- review ---

    def _review(self, t: Task) -> Settled:
        """The review kind: the panel reads the given input and its findings are the result.

        There is nothing to rework and nothing to merge: no gates are run (a review task has no acceptance
        and no allowed files) and the copy is left exactly as it was prepared. `ahub accept` closes the task
        like a scout's.
        """
        t = self._prepare(t)
        inp = str(t.limits.get("input") or "")
        try:
            material = review_material(self.project, t.worktree, inp)
            if not material.strip():
                return self._settle(State.NEEDS_DECISION,
                                    reasons.dump("review_input", err=_t("engine.review_empty", input=inp)))
        except ReviewInputError as e:
            return self._settle(State.NEEDS_DECISION, reasons.dump("review_input", err=e))
        models = self._panel_refs(t) or [self._exec_ref(t)]
        round_no = max(1, t.round)
        rework_notes = str(t.limits.get("rework_notes") or "")
        if rework_notes:
            t.limits.pop("rework_notes", None)
            lim = dict(self.task().limits)
            lim.pop("rework_notes", None)
            self.store.update_task(t.id, limits=lim)
        self.set_phase(Phase.STUDYING)
        _, rev_summary, _ = prompts.assemble_guidance(self.project, "review")
        self._record_prompts(t, rev_summary)
        t = self.move(State.REVIEWING, reasons.dump("review_round", round=round_no))
        # no gates of a code task here: an empty result, so nothing pretends a test ran
        decision, reason, findings = self._review_round(t, gates.GateResult(base="", head=""), models, round_no,
                                                        max(1, int(t.review.get("rounds") or 1)),
                                                        material=material, rework=False, notes=rework_notes)
        if decision == "queued":
            return self._settle(State.QUEUED, reason)
        if decision == "decision":  # a reviewer without a verdict, a stop, the budget
            return self._settle(State.NEEDS_DECISION, reason, payload={"findings": len(findings)})
        summary = _findings_summary(findings)
        report = self._write_review(t, findings, summary, models)
        return self._settle(State.DONE, reason, payload={"summary": summary, "findings": len(findings),
                                                         "report_bytes": report})

    def _write_review(self, t: Task, findings: list[review.Finding], summary: str, models: list[str]) -> int:
        """The result of a review task, written by the hub: the reviewers only wrote verdicts.

        report.md — the findings grouped by severity (file:line, issue, fix); result.json — the usual shape.
        Returns the size of the report.
        """
        base = Path(t.worktree) / workspace.AHUB_DIR
        sections = [f"{prompts.report_heading()}\n{summary}"]
        if findings:
            def _loc(f: review.Finding) -> str:
                return f"`{f.file}:{f.line}`" if f.line is not None else f"`{f.file}`"

            by_sev = [f"### {s}\n" + "\n".join(
                f"- {_loc(f)} — {f.issue}" + (f"\n  fix: {f.fix}" if f.fix else "")
                for f in findings if f.severity == s)
                for s in ("high", "medium", "low") if any(f.severity == s for f in findings)]
            sections.append(f"## {_t('engine.findings_head')}\n" + "\n\n".join(by_sev))
        text = "\n\n".join(sections) + "\n"
        (base / "report.md").write_text(text, encoding="utf-8")
        result = {"summary": summary, "status": "done",
                  "notes": _t("engine.review_notes", models=", ".join(models))}
        (base / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                                          encoding="utf-8")
        return len(text.encode())

    # --- code and routine ---

    def _budget_stop(self, role: Role, alias: str, sid: str | None) -> Settled:
        """Budget spent: worker saves progress in one short step, task goes to "Needs decision"."""
        self.budget_hit = False  # allow one short "save and stop" step
        if sid:
            self.session(role, alias, prompts.stop_prompt(), session_id=sid, log_name=role.value,
                         prompt_kind="stop", stop_predicate=self.stop_requested)
        go, usd = self.task_cost()
        self.store.add_event("budget_hard", task_id=self.task_id, project=self.project.name,
                             payload={"go": round(go, 4), "usd": round(usd, 4)})
        t = self.task()
        return self._settle(State.NEEDS_DECISION, reasons.dump("budget_spent", go=f"{go:.3f}",
                                                               budget=f"{t.budget_go:g}"))

    def _prepare_code(self, t: Task) -> Task:
        if t.state is State.PREPARING:
            try:
                p = prepare.prepare(self.project, t)
            except prepare.PrepareError as e:
                raise _Settle(State.ERROR, reasons.dump("prepare_failed", err=e)) from e
            fields = {"worktree": p.workspace.path, "branch": p.workspace.branch, "round": max(1, t.round)}
            if not t.base_sha:
                fields["base_sha"] = p.workspace.base_sha
            to = State.FIXING if t.round > 1 else State.WORKING
            t = self.move(to, reasons.dump("worker_started"), fields=fields)
        return t

    def _after_gates(self, t: Task, g: gates.GateResult, models: list[str], round_no: int,
                     max_rounds: int, role: Role, sid: str | None):
        """Review stage after green gates (or a quota-hold resume): settle, or fix-continuation.

        Returns Settled, or (task, prompt, kind, round_no, sid) for the next worker turn.
        """
        summary = self._result(t).get("summary", "")
        payload = {"summary": str(summary)[:500], "diffstat": g.diffstat,
                   "tests": "green" if g.tests_ok else ("none" if g.tests_ok is None else "red")}
        if not models:
            code = "gates_passed" if t.kind is Kind.ROUTINE else "gates_passed_tests"
            return self._settle(State.DONE, reasons.dump(code), payload=payload)
        if self.over_budget():
            return self._budget_stop(role, self._exec_ref(), sid)
        t = self.move(State.REVIEWING, reasons.dump("review_round", round=round_no))
        decision, reason, findings = self._review_round(t, g, models, round_no, max_rounds)
        if decision == "queued":
            return self._settle(State.QUEUED, reason)
        if decision == "done":
            return self._settle(State.DONE, reason, payload=payload)
        if decision == "decision":
            return self._settle(State.NEEDS_DECISION, reason,
                                payload={**payload, "findings": len(findings)})
        round_no += 1
        t = self.move(State.FIXING, reason, fields={"round": round_no})
        prompt, kind = review.fix_prompt(findings), "rework"
        return t, prompt, kind, round_no, sid

    def _code(self, t: Task) -> Settled:
        try:
            t = self._prepare_code(t)
        except _Settle as s:
            return self._settle(s.state, s.reason)
        role = Role.EXECUTOR if t.kind is Kind.CODE else Role.ROUTINE
        models = self._panel_refs(t)
        max_rounds = max(1, int(t.review.get("rounds") or 1))
        prev = [s for s in self.store.list_sessions(t.id) if s.role == role.value and s.external_id]
        sid = prev[-1].external_id if prev else None
        notes = str(t.limits.get("rework_notes") or "")
        fresh = bool(t.limits.get("fresh_session")) or not sid
        if notes:
            t.limits.pop("rework_notes", None)
            lim = dict(self.task().limits)
            lim.pop("rework_notes", None)
            self.store.update_task(t.id, limits=lim)
        self._clear_fresh(t)
        # a lock-wait requeue resumes straight at the gates — the worker turn already happened.
        # A fresh session or pending rework notes still need a real turn, so they overrule the skip.
        # The flag is cleared either way once consumed.
        lock_resume = bool(self.task().limits.get("lock_wait")) and not fresh and not notes
        if self.task().limits.get("lock_wait"):
            t.limits.pop("lock_wait", None)
            lim = dict(self.task().limits)
            lim.pop("lock_wait", None)
            self.store.update_task(t.id, limits=lim)
        round_no = t.round
        skip_turn = lock_resume
        # a quota hold about the review resumes straight at the review panel — the worker turn
        # and the gates already happened. A fresh session, pending rework notes or a lock-wait
        # resume still goes through the normal flow (each stage's quota gates its own work).
        resume = self._quota_resume(t) if not (fresh or notes or lock_resume) else None
        if resume is not None:
            g0, models, round_no, max_rounds = resume
            if self.over_budget():
                return self._budget_stop(role, self._exec_ref(), sid)
            t = self.move(State.REVIEWING, reasons.dump("review_round", round=round_no))
            out = self._after_gates(t, g0, models, round_no, max_rounds, role, sid)
            if isinstance(out, Settled):
                return out
            t, prompt, kind, round_no, sid = out
        else:
            if not lock_resume:
                held, swapped = self._quota_gate_executor(t)
                if held is not None:
                    return held
                if swapped:  # a fallback model starts from scratch: full brief, no resume
                    t = self.task()
                    sid, fresh = None, True
            if fresh:  # new session (different model/brief, or no session before): full brief + instructions
                prompt, summary, _ = prompts.code_prompt(self.project, t)
                self._record_prompts(t, summary)
                kind = "start"
                if notes:
                    prompt += f"\n\n{prompts.orchestrator_heading(rework=True)}\n" + notes
                    kind = "rework"
                sid = None
            elif notes:
                prompt, kind = review.fix_prompt([], notes=notes), "rework"
            else:
                prompt, kind = prompts.CONTINUE_PROMPT, "continue"
        while True:
            if skip_turn:
                skip_turn = False
                t = self.move(State.CHECKING, reasons.dump("gates"))
            else:
                self.set_phase(Phase.WRITING)
                self._saw_tools = False
                r, final = self._step_with_continue(role, self._exec_ref(t), prompt, session_id=sid,
                                                    log_name=role.value, prompt_kind=kind)
                sid = r.session_id or sid
                if kind == "continue" or kind in ("start", "rework"):
                    stuck = self._stuck_guard(kind, sid, r)
                    if stuck is not None:
                        return stuck
                if final is not None:
                    if self.budget_hit:
                        return self._budget_stop(role, self._exec_ref(), sid)
                    return self._settle(*final)
                blocked = self._blocked(t)
                if blocked:
                    return self._settle(State.NEEDS_DECISION, reasons.dump("blocked", summary=blocked[:400]))
                t = self.move(State.CHECKING, reasons.dump("gates"))
            sid, sync_final = self._sync_before_gates(t, role, self._exec_ref(t), sid)
            if sync_final is not None:
                if self.budget_hit:
                    return self._budget_stop(role, self._exec_ref(), sid)
                return self._settle(*sync_final)
            t = self.task()
            try:
                g = self._gate(t)
            except gates.LockTimeout as e:
                return self._lock_wait(e)
            fixed_once = False
            while True:
                if g.fatal:
                    return self._settle(State.NEEDS_DECISION,
                                        reasons.dump("gates_blocked", problems=gates.codes(g.fatal)),
                                        payload={"diffstat": g.diffstat})
                problem = "; ".join(g.repairable) if g.repairable else ""
                if not problem and g.tests_ok is False:
                    problem = _t("engine.accept_red")
                if not problem:
                    break
                if fixed_once:
                    return self._settle(State.NEEDS_DECISION,
                                        reasons.dump("gates_failed", problems=_gate_problems(g)),
                                        payload={"tests_tail": g.tests_tail[-800:]})
                fixed_once = True
                red_tests = g.tests_ok is False and not g.repairable
                fix = (review.fix_prompt([], gate=g) if red_tests else prompts.repair_prompt(problem))
                fix_kind = "rework" if red_tests else "repair"
                self._saw_tools = False
                r, final = self._step_with_continue(role, self._exec_ref(), fix, session_id=sid,
                                                    log_name=role.value,
                                                    prompt_kind=fix_kind)
                sid = r.session_id or sid
                stuck = self._stuck_guard(fix_kind, sid, r)
                if stuck is not None:
                    return stuck
                if final is not None:
                    if self.budget_hit:
                        return self._budget_stop(role, self._exec_ref(), sid)
                    return self._settle(*final)
                try:
                    g = self._gate(self.task())
                except gates.LockTimeout as e:
                    return self._lock_wait(e)
            out = self._after_gates(t, g, models, round_no, max_rounds, role, sid)
            if isinstance(out, Settled):
                return out
            t, prompt, kind, round_no, sid = out

    def _record_prompts(self, t: Task, summary: str) -> None:
        lim = dict(self.task().limits)
        lim["prompts"] = summary
        t.limits["prompts"] = summary
        self.store.update_task(t.id, limits=lim)

    def _clear_fresh(self, t: Task) -> None:
        if t.limits.get("fresh_session"):
            t.limits.pop("fresh_session", None)
            lim = dict(self.task().limits)
            lim.pop("fresh_session", None)
            self.store.update_task(t.id, limits=lim)

    def _quota_clear(self) -> None:
        """Drop the quota-hold marker: provider work is starting, the hold episode is over."""
        lim = self.task().limits
        if "quota_hold" in lim:
            lim = dict(lim)
            lim.pop("quota_hold", None)
            self.store.update_task(self.task_id, limits=lim)

    def _review_hold_git(self, t: Task, g: gates.GateResult, material: str | None) -> dict | None:
        """What a review hold stores so the resume skips the gates: the HEAD they passed on."""
        if material is not None or t.kind not in (Kind.CODE, Kind.ROUTINE):
            return None
        commit = str(self._result(t).get("commit", "") or "")
        if not commit:
            return None
        return {"head": g.head, "commit": commit, "base": g.base, "tests_ok": g.tests_ok,
                "tests_tail": g.tests_tail[:4000], "diffstat": g.diffstat}

    def _quota_hold(self, stage: str, reason: str, bucket, *, git: dict | None = None,
                   err: str = "") -> None:
        """Record a quota hold: resume info, the backoff wake time, and the one owner notice.

        `git` (review holds of code/routine tasks) carries what the skipped gates saw, so the
        resume can check result.json commit == HEAD instead of re-running them. Refreshing a hold
        keeps `since`/`notified`: one hold episode tells the owner once.
        """
        from ahub import quota as _quota

        now = now_ms()
        prev = self.task().limits.get("quota_hold") or {}
        prev = prev if isinstance(prev, dict) else {}
        n = int(prev.get("n") or 0) + 1
        reset = int(bucket.reset_at) if bucket is not None and getattr(bucket, "reset_at", 0) else 0
        marker: dict = {"stage": stage, "reason": reason, "since": int(prev.get("since") or now),
                        "not_before": _quota.hold_wake_ms(now, reset, n), "n": n,
                        "notified": bool(prev.get("notified"))}
        if git:
            marker.update(git)
        marker["notice"] = self._hold_notice(stage, bucket, err)
        lim = dict(self.task().limits)
        lim["quota_hold"] = marker
        self.store.update_task(self.task_id, limits=lim)

    def _hold_notice(self, stage: str, bucket, err: str) -> str:
        """The one DECISION text for a long hold (language-neutral params only)."""
        t = self.task()
        if stage == "review":
            setting, cmd = "fallback_reviewer", f"ahub task edit {t.label} --review <alias>"
        else:
            setting, cmd = "fallback_executor", f"ahub model {t.label} <alias>"
        if bucket is not None:
            pct = int(round(bucket.remaining * 100))
            return reasons.dump("quota_hold", group=bucket.group, window=bucket.window, pct=pct,
                                reset=fmt_local(bucket.reset_at), setting=setting, cmd=cmd)
        return reasons.dump("quota_hold_err", err=(err or "")[:120], setting=setting, cmd=cmd)

    def _quota_gate_executor(self, t: Task) -> tuple[Settled | None, bool]:
        """Executor quota before any executor turn: (hold to settle, fallback swapped).

        A hold settles the task with no turn spent; a swap continues on the fallback model.
        """
        from ahub import config as _config
        from ahub import quota as _quota

        hub_cfg = _config.load_hub()
        qres = _quota.check_quota_for_models(self.store, [self._exec_ref(t)], hub_cfg.quota, {},
                                             role=Role.EXECUTOR, label=t.label)
        if qres.ok:
            return None, False
        if qres.fallback_model:
            old = _quota.swap_executor(self.store, t, qres.fallback_model)
            self._quota_clear()  # a fresh model does the work from scratch
            self.store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                                 payload={"from": old, "to": qres.fallback_model, "text": qres.event_text})
            return None, True
        self._quota_hold(_quota.HOLD_STAGE_EXECUTOR, qres.wait_reason, qres.breached_bucket)
        return self._settle(State.QUEUED, qres.wait_reason), False

    def _quota_resume(self, t: Task) -> tuple[gates.GateResult, list[str], int, int] | None:
        """A review-stage hold with the work still delivered: resume straight at the review panel.

        The gate result is rebuilt from what the hold stored (base, tests) — the worker turn and
        the gates do not re-run. None — run normally (stale head, pending worker input, no panel).
        """
        hold = self.task().limits.get("quota_hold") or {}
        if not isinstance(hold, dict) or hold.get("stage") != "review":
            return None
        if t.kind not in (Kind.CODE, Kind.ROUTINE):
            return None
        if self.task().limits.get("fresh_session") or self.task().limits.get("rework_notes"):
            return None
        models = self._panel_refs(t)
        if not models:
            return None
        wt = t.worktree
        if not wt or not Path(wt).is_dir():
            return None
        try:
            head = workspace.head(wt)
            dirty = workspace.changed_files(wt)
            work = workspace.git(self.project.root, "rev-parse", "--verify", self.project.work_branch,
                                 check=False)
            ref = work.stdout.strip()
            synced = bool(ref) and workspace.git(wt, "merge-base", "--is-ancestor", ref, "HEAD",
                                                 check=False).returncode == 0
        except (workspace.WorkspaceError, OSError, subprocess.TimeoutExpired):
            return None  # the copy is unreadable: the normal run reports it properly
        if not head or head != hold.get("head"):
            return None
        commit = str(self._result(t).get("commit", "") or "")
        if not commit or commit[:7] != head[:7] or commit[:7] != str(hold.get("commit", ""))[:7]:
            return None
        if dirty:
            return None
        if not synced:
            return None  # the work branch moved: the normal run syncs and re-runs the gates
        g = gates.GateResult(base=str(hold.get("base") or t.base_sha or ""), head=head,
                             diffstat=str(hold.get("diffstat") or ""), tests_ok=hold.get("tests_ok"),
                             tests_tail=str(hold.get("tests_tail") or ""))
        return g, models, t.round, max(1, int(t.review.get("rounds") or 1))

    def _blocked(self, t: Task) -> str:
        res = self._result(t)
        return str(res.get("summary", "")) if res.get("status") == "blocked" else ""

    def _gate(self, t: Task) -> gates.GateResult:
        self.set_phase(Phase.TESTING)

        def on_wait():
            self.set_phase(Phase.WAITING)

        orch = bool(t.limits.get("orch_edit"))
        return gates.check(self.project, t, orch_edit=orch, on_wait=on_wait, should_stop=self.stop_requested)

    def _sync_before_gates(self, t: Task, role: Role, alias: str,
                            sid: str | None) -> tuple[str | None, tuple[State, str] | None]:
        """Merge the work branch into the task copy before the gates (each round).

        Clean merge (or already up to date) — continue, the merge commit is the task's and the gates
        read the new merge-base. Conflict — abort and run one resolve turn in the same session,
        then the gates. Returns (new session id, final to settle or None).
        """
        wt = t.worktree
        if not wt or not Path(wt).is_dir():
            return sid, None
        work = workspace.git(self.project.root, "rev-parse", "--verify", self.project.work_branch,
                             check=False)
        ref = work.stdout.strip()
        if work.returncode != 0 or not ref:
            self.log.warning("sync: no work branch %s", self.project.work_branch)
            return sid, None
        if workspace.git(wt, "merge-base", "--is-ancestor", ref, "HEAD", check=False).returncode == 0:
            return sid, None
        m = workspace.git(wt, "merge", "--no-edit", ref, check=False)
        if m.returncode == 0:
            self.log.info("synced %s into %s", self.project.work_branch, t.label)
            self._refresh_result_commit(t)
            return sid, None
        unmerged = workspace.git(wt, "diff", "--name-only", "--diff-filter=U",
                                 check=False).stdout.split()
        workspace.git(wt, "merge", "--abort", check=False)
        if not unmerged:
            self.log.warning("sync merge failed (not a conflict): %s", (m.stderr or m.stdout)[-300:])
            return sid, None
        self.log.info("sync conflict, asking worker to resolve: %s", ", ".join(unmerged[:5]))
        prompt = prompts.sync_conflict_prompt(self.project)
        r, final = self._step_with_continue(role, alias, prompt, session_id=sid, log_name=role.value,
                                            prompt_kind="rework")
        new_sid = r.session_id or sid
        if final is not None:
            return new_sid, final
        blocked = self._blocked(self.task())
        if blocked:
            return new_sid, (State.NEEDS_DECISION, reasons.dump("blocked", summary=blocked[:400]))
        return new_sid, None

    def _refresh_result_commit(self, t: Task) -> None:
        """Point result.json at the sync merge commit, so the gates see the hub's merge as the task's."""
        try:
            path = Path(t.worktree) / workspace.AHUB_DIR / "result.json"
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict) or not str(data.get("commit", "")).strip():
            return
        try:
            head = workspace.head(t.worktree)
        except workspace.WorkspaceError:
            return
        if str(data.get("commit")) != head:
            data["commit"] = head
            try:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as e:
                self.log.warning("result commit not refreshed: %s", e)

    def _lock_wait(self, e: gates.LockTimeout) -> Settled:
        """A busy test lock is a wait, not a red acceptance: back to the queue, gates re-run."""
        if getattr(e, "stopped", False) or self.stop_requested():
            return self._settle(State.STOPPED, reasons.dump("stopped"))
        self.set_phase(Phase.WAITING)
        self._quota_clear()  # the lock resume owns the re-pick, not the quota backoff
        lim = dict(self.task().limits)
        lim["lock_wait"] = True  # the resume skips the worker turn and goes straight to the gates
        self.store.update_task(self.task_id, limits=lim)
        return self._settle(State.QUEUED, reasons.dump("wait_test_lock"))

    def _review_round(self, t: Task, g: gates.GateResult, models: list[str], round_no: int,
                      max_rounds: int, *, material: str | None = None,
                      rework: bool = True, notes: str = "") -> tuple[str, str, list]:
        """One round of the panel: the reviewer sessions, the verdict repair retry, the decision.

        `material` — what is under review (None — the diff of the copy, a code task's own);
        `rework` False — a review task: the findings are the result, every one of them, the low ones too.
        """
        from concurrent.futures import ThreadPoolExecutor

        diff = gates.diff_text(t.worktree, g.base) if material is None else material
        from ahub import config, quota
        from ahub.time import fmt_local

        hub_cfg = config.load_hub()
        quota_cfg = hub_cfg.quota
        fallback_rev = quota_cfg.fallback_reviewer or quota_cfg.fallback
        if not fallback_rev:  # no configured fallback: the next reviewer-menu entry above
            # threshold takes the turn instead of waiting (quotas are cached, no probe here)
            fallback_rev = quota.menu_reviewer_fallback(self.store, self.project, quota_cfg,
                                                        exclude=models, hub=hub_cfg)
        new_models = []
        for m in models:
            _prov, buckets = quota.get_model_buckets(self.store, m)
            breached = quota.breached(buckets, quota_cfg, now_ms())

            if breached:
                pct = int(round(breached.remaining * 100))
                if fallback_rev:
                    event_text = _t("engine.quota_fallback", label=t.label, group=breached.group,
                                    window=breached.window, pct=pct, fallback=fallback_rev)
                    self.store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                                         payload={"from": m, "to": fallback_rev, "text": event_text})
                    new_models.append(fallback_rev)
                else:
                    reset_str = fmt_local(breached.reset_at)
                    reason = reasons.dump("wait_quota", group=breached.group, window=breached.window,
                                          pct=pct, reset=reset_str)
                    self._quota_hold(quota.HOLD_STAGE_REVIEW, reason, breached,
                                     git=self._review_hold_git(t, g, material))
                    return "queued", reason, []
            else:
                new_models.append(m)
        models = new_models

        for m in models:
            review.review_path(t.worktree, round_no, m).unlink(missing_ok=True)

        prompts_by_model: dict[str, str] = {}
        summaries_by_model: dict[str, str] = {}
        for m in models:
            p, s, _ = review.review_prompt(self.project, t, diff, g, round_no, m, notes=notes)
            prompts_by_model[m] = p
            summaries_by_model[m] = s

        def one(m: str):
            prompt = prompts_by_model[m]
            # a reviewer is not interrupted by a nudge: the message waits for the executor's next turn
            return self.session(Role.REVIEWER, m, prompt, keep_session_on_retry=False,
                                log_name=f"reviewer_r{round_no}_{m}", prompt_kind="review",
                                stop_predicate=self.stop_requested, prompts_summary=summaries_by_model[m])

        with ThreadPoolExecutor(max_workers=len(models)) as ex:
            results = list(ex.map(one, models))
        if (stop := self._review_interrupted(results)) is not None:
            return stop
        for m, r in zip(models, results, strict=True):
            if r.outcome is Outcome.QUOTA:
                _prov, buckets = quota.get_model_buckets(self.store, m, force=True)
                reason, event_text = quota.describe_error(t.label, buckets, r.error, fallback_rev)
                if fallback_rev:
                    self.store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                                         payload={"from": m, "to": fallback_rev, "text": event_text})
                    rev = dict(t.review)
                    rev["models"] = [fallback_rev if x == m else x for x in models]
                    self.store.update_task(t.id, review=rev)
                    quota.expire_hold(self.store, t)
                else:
                    self._quota_hold(quota.HOLD_STAGE_REVIEW, reason, quota.pick_window(buckets),
                                     git=self._review_hold_git(t, g, material), err=r.error)
                return "queued", reason, []
        self._revert_reviewer(t)
        by_model = dict(zip(models, results, strict=True))
        found: dict[str, review.Review] = {}
        for m in models:
            rv = review.parse(review.review_path(t.worktree, round_no, m), m)
            if rv is not None:
                found[m] = rv
        missing = [m for m in models if m not in found and by_model[m].session_id]
        if missing:  # reviewer with no verdict gets exactly one same-session retry
            def retry_one(m: str):
                return self.session(Role.REVIEWER, m, review.verdict_repair_prompt(round_no, m),
                                    session_id=by_model[m].session_id, keep_session_on_retry=False,
                                    log_name=f"reviewer_r{round_no}_{m}", prompt_kind="review",
                                    stop_predicate=self.stop_requested)

            with ThreadPoolExecutor(max_workers=len(missing)) as ex:
                retries = list(ex.map(retry_one, missing))
            if (stop := self._review_interrupted(retries)) is not None:
                return stop
            for m, r in zip(missing, retries, strict=True):
                if r.outcome is Outcome.QUOTA:
                    _prov, buckets = quota.get_model_buckets(self.store, m, force=True)
                    reason, event_text = quota.describe_error(t.label, buckets, r.error, fallback_rev)
                    if fallback_rev:
                        self.store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                                             payload={"from": m, "to": fallback_rev, "text": event_text})
                        rev = dict(t.review)
                        rev["models"] = [fallback_rev if x == m else x for x in models]
                        self.store.update_task(t.id, review=rev)
                        quota.expire_hold(self.store, t)
                    else:
                        self._quota_hold(quota.HOLD_STAGE_REVIEW, reason, quota.pick_window(buckets),
                                         git=self._review_hold_git(t, g, material), err=r.error)
                    return "queued", reason, []
            self._revert_reviewer(t)
            for m in missing:
                rv = review.parse(review.review_path(t.worktree, round_no, m), m)
                if rv is not None:
                    found[m] = rv
        reviews = [found[m] for m in models if m in found]
        decision, reason = review.panel(reviews, models, round_no, max_rounds, rework=rework)
        if not rework:  # a review task reports what every reviewer wrote, low findings included
            return decision, reason, review.dedup([f for rv in reviews for f in rv.findings])
        return decision, reason, review.blocking_findings(reviews)

    def _review_interrupted(self, results: list) -> tuple[str, str, list] | None:
        """Reviewer session stopped: lost lease — raise, budget or stop — "needs decision"."""
        if not any(r.outcome is Outcome.KILLED for r in results):
            return None
        if self.lost.is_set():
            raise LeaseLost()
        code = "review_budget" if self.budget_hit else "review_stopped"
        return "decision", reasons.dump(code), []

    def _revert_reviewer(self, t: Task) -> None:
        """Reviewer must not touch files — revert."""
        changed = workspace.changed_files(t.worktree)
        if changed:
            self.log.warning("reviewer changed files, reverting: %s", changed[:5])
            workspace.git(t.worktree, "checkout", "--", ".", check=False)
            workspace.git(t.worktree, "clean", "-fd", "-e", workspace.AHUB_DIR, check=False)


class _Settle(Exception):
    def __init__(self, state: State, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason
