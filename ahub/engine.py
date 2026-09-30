"""Движок задачи: владелец ведёт задачу от очереди до «Готово / Нужно решение / Ошибка / Остановлена».

Один экземпляр движка = один процесс задачи (ahub.worker). Он:
- берёт аренду задачи и продлевает её в фоне; потерял аренду — немедленно прекращает работу без записи состояния;
- ведёт этапы и пишет их в хранилище (владелец — единственный писатель активной задачи);
- запускает сессии поставщика через общий раннер и решает, что делать с итогом хода (architecture §6.3):
  сбой сети → повтор с паузой (та же сессия, если известна); тишина → одно продолжение той же сессией, потом
  «Нужно решение»; квота/таймаут → «Нужно решение»; нет доступа/ошибка модели/падение → «Ошибка»;
  остановка по просьбе → «Остановлена».
V08: разведка. Код/рутина/ревью (ворота, панель, слияние) — M3.
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
from ahub import gates, prepare, prompts, providers, registry, review, transitions, workspace
from ahub.config import ProjectConfig
from ahub.model import ACTIVE, Kind, Phase, Role, State
from ahub.providers.base import Act, Activity, Outcome, RunResult, RunSpec
from ahub.providers.runner import run as run_session
from ahub.store import Store, Task
from ahub.time import now_ms

LEASE_MS = 90_000
STOP_POLL_S = 3.0
BUDGET_POLL_S = 30.0
REPORT_MAX_BYTES = 18_000  # 12 КБ по контракту + запас; больше — не отказ, а пометка

_WRITE_TOOLS = {"edit", "write", "patch", "multiedit", "apply_patch"}
_READ_TOOLS = {"read", "grep", "glob", "list", "webfetch", "websearch"}


class LeaseLost(RuntimeError):
    """Аренду забрали: прекратить работу, состояние не трогать."""


def owner_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass
class Settled:
    """Чем кончился шаг движка (для тестов и логов)."""

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
        self._cur = None  # (поставщик, id сессии) идущего хода — для живого учёта бюджета
        self.log = hublog.get("engine", task=self.task_id, project=project.name)

    # --- аренда ---

    def _keeper(self, done: threading.Event) -> None:
        interval = max(1.0, self.lease_ms / 3000)
        while not done.wait(interval):
            try:
                ok = transitions.renew(self.store, self.task_id, self.owner, lease_ms=self.lease_ms)
            except sqlite3.Error:
                self.log.exception("продление аренды упало")
                continue
            if not ok:
                self.log.warning("аренда потеряна — прекращаю работу")
                self.lost.set()
                return

    def run(self) -> Settled:
        if not transitions.acquire(self.store, self.task_id, self.owner, pid=os.getpid(), lease_ms=self.lease_ms):
            self.log.info("задача занята другим владельцем — выхожу")
            t = self.store.get_task(self.task_id)
            return Settled(t.state if t else State.ERROR, "занята")
        done = threading.Event()
        keeper = threading.Thread(target=self._keeper, args=(done,), daemon=True)
        keeper.start()
        try:
            return self._run()
        except LeaseLost:
            t = self.store.get_task(self.task_id)
            return Settled(t.state if t else State.ERROR, "аренда потеряна")
        except workspace.WorkspaceError as e:
            self.log.error("рабочая копия: %s", e)
            return self._settle(State.ERROR, f"рабочая копия: {e}")
        except Exception as e:
            self.log.exception("движок упал")
            try:
                return self._settle(State.ERROR, f"сбой хаба: {type(e).__name__}: {e}"[:500])
            except Exception:
                self.log.exception("не удалось записать ошибку")
                raise
        finally:
            done.set()
            keeper.join(timeout=5)
            try:
                transitions.release(self.store, self.task_id, self.owner)
            except sqlite3.Error:
                self.log.exception("release упал")

    # --- помощники ---

    def task(self) -> Task:
        t = self.store.get_task(self.task_id)
        if t is None:
            raise RuntimeError(f"нет задачи T{self.task_id}")
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
            return Settled(t.state, t.state_reason)  # уже решено (например, остановлена)
        cost = self.task_cost()
        body = {"cost_go": round(cost[0], 4), "cost_usd": round(cost[1], 4)}
        body.update(payload or {})
        self.move(to, reason[:500], payload=body)
        self.log.info("итог: %s%s", to.value, f" ({reason[:200]})" if reason else "")
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
        """Бюджет задачи (вся задача, вкл. ревью): 80 % — событие в журнал; 100 % — True."""
        t = self.task()
        go, usd = self.task_cost()
        if live and self._cur is not None:
            prov, sid = self._cur
            try:
                u = prov.usage(sid)
            except Exception:
                u = None
            if u is not None:
                row = next((s for s in self.store.list_sessions(self.task_id)
                            if s.external_id == sid and s.provider == prov.name), None)
                go += max(0.0, (u.cost_go or 0.0) - (row.cost_go if row else 0.0))
                usd += max(0.0, (u.cost_usd or 0.0) - (row.cost_usd if row else 0.0))
        over = (t.budget_go > 0 and go >= t.budget_go) or (usd > t.budget_usd)
        if not over and not self._soft_sent and t.budget_go > 0 and go >= 0.8 * t.budget_go:
            self._soft_sent = True
            self.store.add_event("budget_soft", task_id=self.task_id, project=self.project.name,
                                 payload={"go": round(go, 4), "budget_go": t.budget_go})
        return over

    def _pause(self, secs: float) -> bool:
        """Прерываемая пауза. False — пока ждали, попросили остановиться."""
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
                self.log.exception("фаза не записана")

    def _on_activity(self, act: Activity) -> None:
        if act.kind in (Act.TOOL_START, Act.TOOL_END):
            tool = act.tool.lower()
            cmd = str(act.data.get("input", {}).get("command", ""))
            if tool == "bash" and "pytest" in cmd:
                self.set_phase(Phase.TESTING)
            elif tool in _WRITE_TOOLS:
                self.set_phase(Phase.WRITING)
            elif tool in _READ_TOOLS or tool == "bash":
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
        """Строка сессии: продолжение — та же строка (id поставщика уникален), новая — новая."""
        if session_id:
            for s in self.store.list_sessions(self.task_id):
                if s.provider == provider and s.external_id == session_id:
                    self.store.update_session(s.id, status="running", outcome="", ended_at=None, log_path=log_path)
                    return s.id
        return self.store.add_session(task_id=self.task_id, provider=provider, role=role.value, model=alias,
                                      round=round_no, external_id=session_id or "", log_path=log_path)

    def session(self, role: Role, alias: str, prompt: str, *, session_id: str | None = None,
                keep_session_on_retry: bool = True, log_name: str = "", schema: dict | None = None,
                cwd: str | None = None) -> RunResult:
        """Один ход работника с повторами при сбое сети (architecture §6.3)."""
        entry = registry.get(self.store, alias)
        prov = providers.get(entry.provider)
        t = self.task()
        cwd = cwd or t.worktree
        tmo = self.project.timeouts
        attempt = 0
        while True:
            self._check_lease()
            log_path = str(Path(cwd) / workspace.AHUB_DIR / "logs" / f"{log_name or role.value}.log")
            row = self._session_row(prov.name, role, alias, t.round, session_id, log_path)

            def on_session(sid: str, _row=row, _prov=prov) -> None:
                self._cur = (_prov, sid)
                try:
                    self.store.update_session(_row, external_id=sid)
                except sqlite3.IntegrityError:
                    self.log.warning("сессия %s уже привязана к другой строке", sid)

            spec = RunSpec(prompt=prompt, cwd=cwd, model_id=entry.model_id, variant=entry.variant,
                           session_id=session_id, log_path=log_path, timeout_s=self._remaining_s(),
                           idle_s=tmo.idle_s, schema=schema)
            if session_id:
                self._cur = (prov, session_id)
            r = run_session(prov, spec, on_activity=self._on_activity, on_session=on_session,
                            on_start=lambda pid, _row=row: self.store.update_session(_row, pid=pid),
                            should_stop=self.stop_requested)
            self._cur = None
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
                                              "pause_s": pause, "text": f"сбой поставщика → повтор {attempt}/"
                                                                        f"{tmo.retry_max} через {int(pause)} с"})
                self.log.warning("сбой поставщика: %s → повтор %d/%d через %d с", r.error[:200], attempt,
                                 tmo.retry_max, pause)
                self.set_phase(Phase.WAITING)
                if not self._pause(pause):
                    return RunResult(Outcome.KILLED, r.session_id, error="остановлено во время паузы")
                if keep_session_on_retry and r.session_id:
                    session_id = r.session_id
                continue
            return r

    # --- ход задачи ---

    def _run(self) -> Settled:
        t = self.task()
        if t.state is State.QUEUED:
            t = self.move(State.PREPARING, "взята в работу")
        elif t.state not in ACTIVE:
            return Settled(t.state, "не в работе")
        limit_min = int(t.limits.get("time_limit_min") or 60)
        self._deadline_ms = now_ms() + limit_min * 60_000
        if self.over_budget():
            return self._settle(State.NEEDS_DECISION, "бюджет исчерпан до начала хода")
        if t.kind is Kind.SCOUT:
            return self._scout(t)
        if t.kind in (Kind.CODE, Kind.ROUTINE):
            return self._code(t)
        return self._settle(State.NEEDS_DECISION, f"тип {t.kind.value} ещё не поддержан движком")

    def _prepare(self, t: Task) -> Task:
        if t.state is State.PREPARING:
            ws = workspace.ensure(self.project, t.id)
            fields = {"worktree": ws.path, "branch": ws.branch}
            if not t.base_sha:
                fields["base_sha"] = ws.base_sha
            t = self.move(State.WORKING, "работник начал", fields={**fields, "round": max(1, t.round)})
        return t

    def _outcome_to_state(self, r: RunResult) -> tuple[State, str] | None:
        """Итог хода, после которого продолжать нечего. None — ход успешен или нужна отдельная обработка."""
        if r.outcome is Outcome.KILLED:
            if self.lost.is_set():
                raise LeaseLost()
            if self.budget_hit:
                return State.NEEDS_DECISION, "бюджет исчерпан"
            return State.STOPPED, "остановлена по просьбе"
        if r.outcome is Outcome.TIMEOUT:
            return State.NEEDS_DECISION, f"лимит времени задачи ({r.error})"
        if r.outcome is Outcome.QUOTA:
            return State.NEEDS_DECISION, f"квота поставщика: {r.error[:300]}"
        if r.outcome is Outcome.TRANSIENT:
            return State.NEEDS_DECISION, f"сбой сети/сервера поставщика, повторы кончились: {r.error[:300]}"
        if r.outcome in (Outcome.NO_ACCESS, Outcome.MODEL_ERROR, Outcome.CRASH, Outcome.NOT_STARTED):
            return State.ERROR, f"{r.outcome.value}: {r.error[:400]}"
        return None

    def _step_with_continue(self, role: Role, alias: str, prompt: str, *, session_id: str | None,
                            log_name: str) -> tuple[RunResult, tuple[State, str] | None]:
        """Ход работника; тишина → одно продолжение той же сессией."""
        r = self.session(role, alias, prompt, session_id=session_id, log_name=log_name)
        if r.outcome is Outcome.SILENCE:
            self.store.add_event("silence", task_id=self.task_id, project=self.project.name,
                                 payload={"secs": r.silence_s, "action": "continue",
                                          "text": f"работник молчал {r.silence_s} с → продолжаю ту же сессию"})
            r = self.session(role, alias, prompts.CONTINUE_PROMPT, session_id=r.session_id or session_id,
                             log_name=log_name)
            if r.outcome is Outcome.SILENCE:
                return r, (State.NEEDS_DECISION, f"работник молчал дважды ({r.silence_s} с)")
        return r, self._outcome_to_state(r)

    # --- разведка ---

    def _scout(self, t: Task) -> Settled:
        t = self._prepare(t)
        self.set_phase(Phase.STUDYING)
        prev = [s for s in self.store.list_sessions(t.id) if s.role == Role.SCOUT.value and s.external_id]
        resume_sid = prev[-1].external_id if prev else None  # подхват после сбоя процесса — та же сессия
        prompt = prompts.CONTINUE_PROMPT if resume_sid else prompts.scout_prompt(self.project, t)
        r, final = self._step_with_continue(Role.SCOUT, t.executor, prompt, session_id=resume_sid,
                                            log_name="scout")
        if final is not None:
            return self._settle(*final)
        problem = self._check_scout(t)
        if problem and not problem.startswith("!"):
            r, final = self._step_with_continue(Role.SCOUT, t.executor, prompts.repair_prompt(problem),
                                                session_id=r.session_id, log_name="scout")
            if final is not None:
                return self._settle(*final)
            problem = self._check_scout(t)
        if problem:
            return self._settle(State.NEEDS_DECISION, f"итог разведки не принят: {problem.lstrip('!')}")
        res = self._result(t)
        if res.get("status") == "blocked":
            return self._settle(State.NEEDS_DECISION, f"работник заблокирован: {res.get('summary', '')}"[:500],
                                payload={"summary": res.get("summary", "")})
        report = Path(t.worktree) / workspace.AHUB_DIR / "report.md"
        return self._settle(State.DONE, "отчёт готов",
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
        """Пусто — итог по форме. «!…» — неисправимо repair'ом (разведка изменила файлы)."""
        changed = workspace.changed_files(t.worktree)
        if changed:
            return "!разведка изменила файлы проекта: " + ", ".join(changed[:10])
        if workspace.commits_since(t.worktree, t.base_sha):
            return "!разведка сделала коммиты"
        base = Path(t.worktree) / workspace.AHUB_DIR
        problems = []
        res_path = base / "result.json"
        if not res_path.exists():
            problems.append("нет .ahub/result.json")
        else:
            res = self._result(t)
            if not res:
                problems.append(".ahub/result.json — не JSON-объект")
            else:
                if not str(res.get("summary", "")).strip():
                    problems.append("в result.json пустой summary")
                if res.get("status") not in ("done", "blocked"):
                    problems.append("в result.json status должен быть done или blocked")
        report = base / "report.md"
        if not report.exists() or not report.read_text(encoding="utf-8", errors="replace").strip():
            if self._result(t).get("status") != "blocked":
                problems.append("нет .ahub/report.md")
        return "; ".join(problems)

    # --- код и рутина ---

    def _budget_stop(self, role: Role, alias: str, sid: str | None) -> Settled:
        """Бюджет исчерпан: работник сохраняет сделанное коротким ходом, задача — «Нужно решение»."""
        self.budget_hit = False  # разрешить один короткий ход «сохрани и остановись»
        if sid:
            self.session(role, alias, prompts.STOP_PROMPT, session_id=sid, log_name=role.value)
        go, usd = self.task_cost()
        self.store.add_event("budget_hard", task_id=self.task_id, project=self.project.name,
                             payload={"go": round(go, 4), "usd": round(usd, 4)})
        t = self.task()
        return self._settle(State.NEEDS_DECISION, f"бюджет исчерпан (${go:.2f} из ${t.budget_go:g})")

    def _prepare_code(self, t: Task) -> Task:
        if t.state is State.PREPARING:
            try:
                p = prepare.prepare(self.project, t)
            except prepare.PrepareError as e:
                raise _Settle(State.ERROR, f"подготовка: {e}")
            fields = {"worktree": p.workspace.path, "branch": p.workspace.branch, "round": max(1, t.round)}
            if not t.base_sha:
                fields["base_sha"] = p.workspace.base_sha
            to = State.FIXING if t.round > 1 else State.WORKING
            t = self.move(to, "работник начал", fields=fields)
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
        if notes:
            prompt = review.fix_prompt([], notes=notes)
            lim = dict(t.limits)
            lim.pop("rework_notes", None)
            self.store.update_task(t.id, limits=lim)
        elif sid and not t.limits.get("fresh_session"):
            prompt = prompts.CONTINUE_PROMPT
        else:
            prompt, sid = prompts.code_prompt(self.project, t), None
        round_no = t.round
        while True:
            self.set_phase(Phase.WRITING)
            r, final = self._step_with_continue(role, t.executor, prompt, session_id=sid, log_name=role.value)
            sid = r.session_id or sid
            if final is not None:
                if self.budget_hit:
                    return self._budget_stop(role, t.executor, sid)
                return self._settle(*final)
            blocked = self._blocked(t)
            if blocked:
                return self._settle(State.NEEDS_DECISION, f"работник заблокирован: {blocked}"[:500])
            t = self.move(State.CHECKING, "ворота")
            g = self._gate(t)
            fixed_once = False
            while True:
                if g.fatal:
                    return self._settle(State.NEEDS_DECISION, "; ".join(g.fatal)[:500],
                                        payload={"diffstat": g.diffstat})
                problem = "; ".join(g.repairable) if g.repairable else ""
                if not problem and g.tests_ok is False:
                    problem = "приёмка красная"
                if not problem:
                    break
                if fixed_once:
                    return self._settle(State.NEEDS_DECISION, f"ворота не пройдены после исправления: {problem}"[:500],
                                        payload={"tests_tail": g.tests_tail[-800:]})
                fixed_once = True
                fix = (review.fix_prompt([], gate=g) if g.tests_ok is False and not g.repairable
                       else prompts.repair_prompt(problem))
                r, final = self._step_with_continue(role, t.executor, fix, session_id=sid, log_name=role.value)
                sid = r.session_id or sid
                if final is not None:
                    if self.budget_hit:
                        return self._budget_stop(role, t.executor, sid)
                    return self._settle(*final)
                g = self._gate(self.task())
            summary = self._result(t).get("summary", "")
            payload = {"summary": str(summary)[:500], "diffstat": g.diffstat,
                       "tests": "зелёная" if g.tests_ok else ("нет" if g.tests_ok is None else "красная")}
            if not models:
                return self._settle(State.DONE, "ворота пройдены" + ("" if t.kind is Kind.ROUTINE else
                                                                     ", приёмка зелёная"), payload=payload)
            if self.over_budget():
                return self._budget_stop(role, t.executor, sid)
            t = self.move(State.REVIEWING, f"ревью, круг {round_no}")
            decision, reason, findings = self._review_round(t, g, models, round_no, max_rounds)
            if decision == "done":
                return self._settle(State.DONE, reason, payload=payload)
            if decision == "decision":
                return self._settle(State.NEEDS_DECISION, reason,
                                    payload={**payload, "findings": len(findings)})
            round_no += 1
            t = self.move(State.FIXING, reason, fields={"round": round_no})
            prompt = review.fix_prompt(findings)

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
                                log_name=f"reviewer_r{round_no}_{m}")

        with ThreadPoolExecutor(max_workers=len(models)) as ex:
            results = list(ex.map(one, models))
        if any(r.outcome is Outcome.KILLED for r in results):
            if self.lost.is_set():
                raise LeaseLost()
            if self.budget_hit:
                return "decision", "бюджет исчерпан во время ревью", []
            return "decision", "остановлено во время ревью", []
        changed = workspace.changed_files(t.worktree)
        if changed:  # ревьюер не должен менять файлы — откатываем
            self.log.warning("ревьюер изменил файлы, откат: %s", changed[:5])
            workspace.git(t.worktree, "checkout", "--", ".", check=False)
            workspace.git(t.worktree, "clean", "-fd", "-e", workspace.AHUB_DIR, check=False)
        reviews = [rv for m in models if (rv := review.parse(review.review_path(t.worktree, round_no, m), m))]
        decision, reason = review.panel(reviews, models, round_no, max_rounds)
        blocking = review.dedup([f for rv in reviews if rv.effective != "approve" for f in rv.findings
                                 if f.severity != "low"])
        return decision, reason, blocking


class _Settle(Exception):
    def __init__(self, state: State, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason
