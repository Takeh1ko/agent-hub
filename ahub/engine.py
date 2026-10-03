"""Task engine: the owner drives a task from queue to "Done / Needs decision / Error / Stopped".

One engine instance = one task process (ahub.worker). It:
- takes the task lease and renews it in the background; lease lost — stops at once without writing state;
- tracks phases and records them in the store (the owner is the only writer of an active task);
- runs provider sessions via the shared runner and decides what to do with each step result (architecture §6.3):
  network failure → retry with pause (same session when known); silence → one same-session nudge, then
  "Needs decision"; quota/timeout → "Needs decision"; no access/model error/crash → "Error";
  requested stop → "Stopped".
V08: scout. Code/routine/review (gates, panel, merge) — M3.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ahub import log as hublog
from ahub import gates, prepare, prompts, providers, registry, review, transitions, transcript, workspace
from ahub.config import ProjectConfig
from ahub.i18n import t as _t
from ahub.model import ACTIVE, Kind, Phase, Role, State
from ahub.providers.base import Act, Activity, Outcome, RunResult, RunSpec
from ahub.providers.runner import run as run_session
from ahub.store import Store, Task
from ahub.time import now_ms

LEASE_MS = 90_000
STOP_POLL_S = 3.0
BUDGET_POLL_S = 30.0
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


def owner_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class Settled:
    """How an engine step ended (for tests and logs)."""

    state: State
    reason: str = ""


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
        self._phase: str = ""
        self._deadline_ms: int | None = None
        self.budget_hit = False
        self._budget_at = 0.0
        self._soft_sent = False
        self.log = hublog.get("engine", task=self.task_id, project=project.name)

    # --- lease ---

    def _keeper(self, done: threading.Event) -> None:
        interval = max(1.0, self.lease_ms / 3000)
        while not done.wait(interval):
            try:
                ok = transitions.renew(self.store, self.task_id, self.owner, lease_ms=self.lease_ms)
            except sqlite3.Error:
                self.log.exception("lease renewal failed")
                continue
            if not ok:
                self.log.warning("lease lost — stopping")
                self.lost.set()
                return

    def run(self) -> Settled:
        if not transitions.acquire(self.store, self.task_id, self.owner, pid=os.getpid(), lease_ms=self.lease_ms):
            self.log.info("task owned by another owner — exiting")
            t = self.store.get_task(self.task_id)
            return Settled(t.state if t else State.ERROR, _t("engine.busy"))
        done = threading.Event()
        keeper = threading.Thread(target=self._keeper, args=(done,), daemon=True)
        keeper.start()
        try:
            return self._run()
        except LeaseLost:
            t = self.store.get_task(self.task_id)
            return Settled(t.state if t else State.ERROR, _t("engine.lease_lost"))
        except workspace.WorkspaceError as e:
            self.log.error("worktree: %s", e)
            return self._settle(State.ERROR, _t("engine.workspace_fail", err=e))
        except Exception as e:
            self.log.exception("engine crashed")
            try:
                return self._settle(State.ERROR, _t("engine.hub_fail", typ=type(e).__name__, err=e)[:500])
            except Exception:
                self.log.exception("failed to record error")
                raise
        finally:
            done.set()
            keeper.join(timeout=5)
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
        t = self.task()
        if t.state is to:
            return Settled(to, reason)
        if t.state not in ACTIVE and t.state is not State.QUEUED:
            return Settled(t.state, t.state_reason)  # already decided (e.g. stopped)
        cost = self.task_cost()
        body = {"cost_go": round(cost[0], 4), "cost_usd": round(cost[1], 4)}
        body.update(payload or {})
        self.move(to, reason[:500], payload=body)
        self.log.info("settled: %s%s", to.value, f" ({reason[:200]})" if reason else "")
        lim = self.task().limits
        if lim.get("orphans"):  # episode done — orphan counter restarts
            lim = dict(lim)
            lim.pop("orphans", None)
            self.store.update_task(self.task_id, limits=lim)
        from ahub import archive

        archive.write_task(self.store, self.project, self.task_id)
        return Settled(to, reason)

    def stop_requested(self) -> bool:
        if self.lost.is_set() or self.budget_hit:
            return True
        now = time.monotonic()
        at, val = self._stop_cache
        if now - at >= STOP_POLL_S:
            try:
                val = self.task().request == "stop"
            except (sqlite3.Error, RuntimeError):
                val = False
            self._stop_cache = (now, val)
            if not val and now - self._budget_at >= BUDGET_POLL_S:
                self._budget_at = now
                if self.over_budget(live=True):
                    self.budget_hit = True
                    return True
        return val

    def over_budget(self, *, live: bool = False) -> bool:
        """Task budget (whole task incl. review): 80% — journal event; 100% — True."""
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

    def _pause(self, secs: float) -> bool:
        """Interruptible pause. False — a stop was requested while waiting."""
        end = time.monotonic() + secs
        while time.monotonic() < end:
            if self.stop_requested():
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
                     log_path: str) -> int:
        """Session row: resume — same row (provider id is unique), new — new row."""
        if session_id:
            for s in self.store.list_sessions(self.task_id):
                if s.provider == provider and s.external_id == session_id:
                    self.store.update_session(s.id, status="running", outcome="", ended_at=None, log_path=log_path)
                    return s.id
        return self.store.add_session(task_id=self.task_id, provider=provider, role=role.value, model=alias,
                                      round=round_no, external_id=session_id or "", log_path=log_path)

    def _note_prompt(self, log_path: str, kind: str, prompt: str) -> None:
        """The prompt of the turn into the sidecar next to the log — `ahub follow` reads it.

        The kinds are transcript.PROMPT_KINDS: start | continue | repair | rework | stop | review.
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
                cwd: str | None = None, prompt_kind: str = "start") -> RunResult:
        """One worker step with retries on network failure (architecture §6.3).

        `prompt_kind` says what the prompt is (start | continue | repair | rework | stop | review) and goes
        into the prompts sidecar, which is what `ahub follow` shows as the turn header.
        """
        entry = registry.get(self.store, alias)
        prov = providers.get(entry.provider)
        t = self.task()
        cwd = cwd or t.worktree
        tmo = self.project.timeouts
        attempt = 0
        while True:
            self._check_lease()
            log_path = str(Path(cwd) / workspace.AHUB_DIR / "logs" / f"{log_name or role.value}.log")
            self._note_prompt(log_path, prompt_kind, prompt)
            row = self._session_row(prov.name, role, alias, t.round, session_id, log_path)

            def on_session(sid: str, _row=row) -> None:
                try:
                    self.store.update_session(_row, external_id=sid)
                except sqlite3.IntegrityError:
                    self.log.warning("session %s already linked to another row", sid)

            spec = RunSpec(prompt=prompt, cwd=cwd, model_id=entry.model_id, variant=entry.variant,
                           session_id=session_id, log_path=log_path, timeout_s=self._remaining_s(),
                           idle_s=tmo.idle_s, schema=schema)
            r = run_session(prov, spec, on_activity=self._on_activity, on_session=on_session,
                            on_start=lambda pid, _row=row: self.store.update_session(_row, pid=pid),
                            should_stop=self.stop_requested)
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
                if not self._pause(pause):
                    return RunResult(Outcome.KILLED, r.session_id, error=_t("engine.pause_killed"))
                if keep_session_on_retry and r.session_id:
                    session_id = r.session_id
                continue
            return r

    # --- task step ---

    def _run(self) -> Settled:
        t = self.task()
        if t.state is State.QUEUED:
            t = self.move(State.PREPARING, _t("engine.taken"))
        elif t.state not in ACTIVE:
            return Settled(t.state, _t("engine.not_active"))
        limit_min = int(t.limits.get("time_limit_min") or 60)
        self._deadline_ms = now_ms() + limit_min * 60_000
        if self.over_budget():
            return self._settle(State.NEEDS_DECISION, _t("engine.budget_before"))
        if t.kind is Kind.SCOUT:
            return self._scout(t)
        if t.kind in (Kind.CODE, Kind.ROUTINE):
            return self._code(t)
        return self._settle(State.NEEDS_DECISION, _t("engine.unsupported", kind=t.kind.value))

    def _prepare(self, t: Task) -> Task:
        if t.state is State.PREPARING:
            ws = workspace.ensure(self.project, t.id)
            fields = {"worktree": ws.path, "branch": ws.branch}
            if not t.base_sha:
                fields["base_sha"] = ws.base_sha
            t = self.move(State.WORKING, _t("engine.worker_started"), fields={**fields, "round": max(1, t.round)})
        return t

    def _outcome_to_state(self, r: RunResult) -> tuple[State, str] | None:
        """Step result after which there is nothing to continue. None — step ok or handled separately."""
        if r.outcome is Outcome.KILLED:
            if self.lost.is_set():
                raise LeaseLost()
            if self.budget_hit:
                return State.NEEDS_DECISION, _t("engine.budget_gone")
            return State.STOPPED, _t("engine.stopped")
        if r.outcome is Outcome.TIMEOUT:
            return State.NEEDS_DECISION, _t("engine.time_limit", err=r.error)
        if r.outcome is Outcome.QUOTA:
            return State.NEEDS_DECISION, _t("engine.quota", err=r.error[:300])
        if r.outcome is Outcome.TRANSIENT:
            return State.NEEDS_DECISION, _t("engine.transient_out", err=r.error[:300])
        if r.outcome in (Outcome.NO_ACCESS, Outcome.MODEL_ERROR, Outcome.CRASH, Outcome.NOT_STARTED):
            return State.ERROR, _t("engine.step_fail", outcome=r.outcome.value, err=r.error[:400])
        return None

    def _step_with_continue(self, role: Role, alias: str, prompt: str, *, session_id: str | None,
                            log_name: str, prompt_kind: str = "start") -> tuple[RunResult, tuple[State, str] | None]:
        """Worker step; silence → one same-session nudge."""
        r = self.session(role, alias, prompt, session_id=session_id, log_name=log_name, prompt_kind=prompt_kind)
        if r.outcome is Outcome.SILENCE:
            self.store.add_event("silence", task_id=self.task_id, project=self.project.name,
                                 payload={"secs": r.silence_s, "action": "continue",
                                          "text": _t("engine.silence_text", secs=r.silence_s)})
            r = self.session(role, alias, prompts.CONTINUE_PROMPT, session_id=r.session_id or session_id,
                             log_name=log_name, prompt_kind="continue")
            if r.outcome is Outcome.SILENCE:
                return r, (State.NEEDS_DECISION, _t("engine.silence_twice", secs=r.silence_s))
        return r, self._outcome_to_state(r)

    # --- scout ---

    def _scout(self, t: Task) -> Settled:
        t = self._prepare(t)
        self.set_phase(Phase.STUDYING)
        prev = [s for s in self.store.list_sessions(t.id) if s.role == Role.SCOUT.value and s.external_id]
        resume_sid = prev[-1].external_id if prev and not t.limits.get("fresh_session") else None  # resume
        self._clear_fresh(t)
        prompt = prompts.CONTINUE_PROMPT if resume_sid else prompts.scout_prompt(self.project, t)
        r, final = self._step_with_continue(Role.SCOUT, t.executor, prompt, session_id=resume_sid,
                                            log_name="scout",
                                            prompt_kind="continue" if resume_sid else "start")
        if final is not None:
            return self._settle(*final)
        problem = self._check_scout(t)
        if problem and not problem.startswith("!"):
            r, final = self._step_with_continue(Role.SCOUT, t.executor, prompts.repair_prompt(problem),
                                                session_id=r.session_id, log_name="scout", prompt_kind="repair")
            if final is not None:
                return self._settle(*final)
            problem = self._check_scout(t)
        if problem:
            return self._settle(State.NEEDS_DECISION, _t("engine.scout_bad", problem=problem.lstrip("!")))
        res = self._result(t)
        if res.get("status") == "blocked":
            return self._settle(State.NEEDS_DECISION, _t("engine.blocked", summary=res.get("summary", ""))[:500],
                                payload={"summary": res.get("summary", "")})
        report = Path(t.worktree) / workspace.AHUB_DIR / "report.md"
        return self._settle(State.DONE, _t("engine.report_done"),
                            payload={"summary": str(res.get("summary", ""))[:500],
                                     "report_bytes": report.stat().st_size})

    def _result(self, t: Task) -> dict:
        p = Path(t.worktree) / workspace.AHUB_DIR / "result.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _check_scout(self, t: Task) -> str:
        """Empty — result matches the form. "!…" — unfixable via repair (scout touched files)."""
        changed = workspace.changed_files(t.worktree)
        if changed:
            return "!" + _t("engine.scout_files", files=", ".join(changed[:10]))
        if workspace.commits_since(t.worktree, t.base_sha):
            return "!" + _t("engine.scout_commits")
        base = Path(t.worktree) / workspace.AHUB_DIR
        problems = []
        res_path = base / "result.json"
        if not res_path.exists():
            problems.append(_t("engine.no_result"))
        else:
            res = self._result(t)
            if not res:
                problems.append(_t("engine.result_not_json"))
            else:
                if not str(res.get("summary", "")).strip():
                    problems.append(_t("engine.empty_summary"))
                if res.get("status") not in ("done", "blocked"):
                    problems.append(_t("engine.bad_status"))
        report = base / "report.md"
        if not report.exists() or not report.read_text(encoding="utf-8", errors="replace").strip():
            if self._result(t).get("status") != "blocked":
                problems.append(_t("engine.no_report"))
        return "; ".join(problems)

    # --- code and routine ---

    def _budget_stop(self, role: Role, alias: str, sid: str | None) -> Settled:
        """Budget spent: worker saves progress in one short step, task goes to "Needs decision"."""
        self.budget_hit = False  # allow one short "save and stop" step
        if sid:
            self.session(role, alias, prompts.stop_prompt(), session_id=sid, log_name=role.value,
                         prompt_kind="stop")
        go, usd = self.task_cost()
        self.store.add_event("budget_hard", task_id=self.task_id, project=self.project.name,
                             payload={"go": round(go, 4), "usd": round(usd, 4)})
        t = self.task()
        return self._settle(State.NEEDS_DECISION, _t("engine.budget_spent", go=f"{go:.3f}",
                                                     budget=f"{t.budget_go:g}"))

    def _prepare_code(self, t: Task) -> Task:
        if t.state is State.PREPARING:
            try:
                p = prepare.prepare(self.project, t)
            except prepare.PrepareError as e:
                raise _Settle(State.ERROR, _t("engine.prepare_fail", err=e))
            fields = {"worktree": p.workspace.path, "branch": p.workspace.branch, "round": max(1, t.round)}
            if not t.base_sha:
                fields["base_sha"] = p.workspace.base_sha
            to = State.FIXING if t.round > 1 else State.WORKING
            t = self.move(to, _t("engine.worker_started"), fields=fields)
        return t

    def _code(self, t: Task) -> Settled:
        try:
            t = self._prepare_code(t)
        except _Settle as s:
            return self._settle(s.state, s.reason)
        role = Role.EXECUTOR if t.kind is Kind.CODE else Role.ROUTINE
        models = list(t.review.get("models") or [])
        max_rounds = max(1, int(t.review.get("rounds") or 1))
        prev = [s for s in self.store.list_sessions(t.id) if s.role == role.value and s.external_id]
        sid = prev[-1].external_id if prev else None
        notes = str(t.limits.get("rework_notes") or "")
        fresh = bool(t.limits.get("fresh_session")) or not sid
        if notes:
            lim = dict(t.limits)
            lim.pop("rework_notes", None)
            self.store.update_task(t.id, limits=lim)
        if fresh:  # new session (different model/brief, or no session before): full brief + instructions
            prompt, kind = prompts.code_prompt(self.project, t), "start"
            if notes:
                prompt += f"\n\n{prompts.orchestrator_heading(rework=True)}\n" + notes
                kind = "rework"
            sid = None
        elif notes:
            prompt, kind = review.fix_prompt([], notes=notes), "rework"
        else:
            prompt, kind = prompts.CONTINUE_PROMPT, "continue"
        self._clear_fresh(t)
        round_no = t.round
        while True:
            self.set_phase(Phase.WRITING)
            r, final = self._step_with_continue(role, t.executor, prompt, session_id=sid, log_name=role.value,
                                                prompt_kind=kind)
            sid = r.session_id or sid
            if final is not None:
                if self.budget_hit:
                    return self._budget_stop(role, t.executor, sid)
                return self._settle(*final)
            blocked = self._blocked(t)
            if blocked:
                return self._settle(State.NEEDS_DECISION, _t("engine.blocked", summary=blocked)[:500])
            t = self.move(State.CHECKING, _t("engine.checking"))
            g = self._gate(t)
            fixed_once = False
            while True:
                if g.fatal:
                    return self._settle(State.NEEDS_DECISION, "; ".join(g.fatal)[:500],
                                        payload={"diffstat": g.diffstat})
                problem = "; ".join(g.repairable) if g.repairable else ""
                if not problem and g.tests_ok is False:
                    problem = _t("engine.accept_red")
                if not problem:
                    break
                if fixed_once:
                    return self._settle(State.NEEDS_DECISION,
                                        _t("engine.gates_retry_fail", problem=problem)[:500],
                                        payload={"tests_tail": g.tests_tail[-800:]})
                fixed_once = True
                red_tests = g.tests_ok is False and not g.repairable
                fix = (review.fix_prompt([], gate=g) if red_tests else prompts.repair_prompt(problem))
                r, final = self._step_with_continue(role, t.executor, fix, session_id=sid, log_name=role.value,
                                                    prompt_kind="rework" if red_tests else "repair")
                sid = r.session_id or sid
                if final is not None:
                    if self.budget_hit:
                        return self._budget_stop(role, t.executor, sid)
                    return self._settle(*final)
                g = self._gate(self.task())
            summary = self._result(t).get("summary", "")
            payload = {"summary": str(summary)[:500], "diffstat": g.diffstat,
                       "tests": "green" if g.tests_ok else ("none" if g.tests_ok is None else "red")}
            if not models:
                return self._settle(State.DONE, _t("engine.gates_passed") + (
                    "" if t.kind is Kind.ROUTINE else _t("engine.gates_passed_tests")), payload=payload)
            if self.over_budget():
                return self._budget_stop(role, t.executor, sid)
            t = self.move(State.REVIEWING, _t("engine.review_round", round=round_no))
            decision, reason, findings = self._review_round(t, g, models, round_no, max_rounds)
            if decision == "done":
                return self._settle(State.DONE, reason, payload=payload)
            if decision == "decision":
                return self._settle(State.NEEDS_DECISION, reason,
                                    payload={**payload, "findings": len(findings)})
            round_no += 1
            t = self.move(State.FIXING, reason, fields={"round": round_no})
            prompt, kind = review.fix_prompt(findings), "rework"

    def _clear_fresh(self, t: Task) -> None:
        if t.limits.get("fresh_session"):
            lim = dict(self.task().limits)
            lim.pop("fresh_session", None)
            self.store.update_task(t.id, limits=lim)

    def _blocked(self, t: Task) -> str:
        res = self._result(t)
        return str(res.get("summary", "")) if res.get("status") == "blocked" else ""

    def _gate(self, t: Task) -> gates.GateResult:
        self.set_phase(Phase.TESTING)

        def on_wait():
            self.set_phase(Phase.WAITING)

        orch = bool(t.limits.get("orch_edit"))
        return gates.check(self.project, t, orch_edit=orch, on_wait=on_wait, should_stop=self.stop_requested)

    def _review_round(self, t: Task, g: gates.GateResult, models: list[str], round_no: int,
                      max_rounds: int) -> tuple[str, str, list]:
        from concurrent.futures import ThreadPoolExecutor

        diff = gates.diff_text(t.worktree, g.base)
        for m in models:
            review.review_path(t.worktree, round_no, m).unlink(missing_ok=True)

        def one(m: str):
            prompt = review.review_prompt(self.project, t, diff, g, round_no, m)
            return self.session(Role.REVIEWER, m, prompt, keep_session_on_retry=False,
                                log_name=f"reviewer_r{round_no}_{m}", prompt_kind="review")

        with ThreadPoolExecutor(max_workers=len(models)) as ex:
            results = list(ex.map(one, models))
        if (stop := self._review_interrupted(results)) is not None:
            return stop
        self._revert_reviewer(t)
        by_model = dict(zip(models, results))
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
                                    log_name=f"reviewer_r{round_no}_{m}", prompt_kind="review")

            with ThreadPoolExecutor(max_workers=len(missing)) as ex:
                retries = list(ex.map(retry_one, missing))
            if (stop := self._review_interrupted(retries)) is not None:
                return stop
            self._revert_reviewer(t)
            for m in missing:
                rv = review.parse(review.review_path(t.worktree, round_no, m), m)
                if rv is not None:
                    found[m] = rv
        reviews = [found[m] for m in models if m in found]
        decision, reason = review.panel(reviews, models, round_no, max_rounds)
        blocking = review.dedup([f for rv in reviews if rv.effective != "approve" for f in rv.findings
                                 if f.severity != "low"])
        return decision, reason, blocking

    def _review_interrupted(self, results: list) -> tuple[str, str, list] | None:
        """Reviewer session stopped: lost lease — raise, budget or stop — "needs decision"."""
        if not any(r.outcome is Outcome.KILLED for r in results):
            return None
        if self.lost.is_set():
            raise LeaseLost()
        return "decision", _t("engine.review_budget" if self.budget_hit else "engine.review_stopped"), []

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
