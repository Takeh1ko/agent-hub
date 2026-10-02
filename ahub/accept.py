"""Решения по результату (V15–V18, architecture §6.7–§6.8): принять/слить, доработать, отклонить, правка оркестратора,
расширение файлов, продление бюджета, смена модели, продолжение с новой постановкой.

Слияние: задача → «принимается» (аренда у того, кто принимает; второй «принять» получит отказ) → ворота на текущем
HEAD копии (правка оркестратора — если HEAD ≠ commit итога работника: законно, с событием) → в корне проекта
`git merge --no-ff` в рабочую ветку → приёмка под ресурсом тестов → красная — откат слияния → push по конфигу →
«принята», хук task_cleanup, копия и ветка удаляются, архив.
"""

from __future__ import annotations

from pathlib import Path

from ahub import archive, events, gates, prepare, registry, tasks, transitions, workspace
from ahub import log as hublog
from ahub.config import ProjectConfig
from ahub.engine import owner_token
from ahub.i18n import t as _t
from ahub.model import CHANGES_FILES, Ev, Kind, State
from ahub.store import Store, Task

_log = hublog.get("accept")


class DecisionError(RuntimeError):
    """Действие невозможно (одна строка для оркестратора)."""


def _get(store: Store, task_id: int) -> Task:
    t = store.get_task(task_id)
    if t is None:
        raise DecisionError(_t("trans.no_task", id=task_id))
    return t


def _root_ready(project: ProjectConfig) -> None:
    cur = workspace.git(project.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != project.work_branch:
        raise DecisionError(_t("accept.root_branch", cur=cur, branch=project.work_branch))
    dirty = [ln for ln in workspace.git(project.root, "status", "--porcelain", "--untracked-files=no").stdout
             .splitlines() if ln.strip()]
    if dirty:
        raise DecisionError(_t("accept.root_dirty", files=", ".join(x[3:] for x in dirty[:5])))


def accept(store: Store, project: ProjectConfig, task_id: int, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.kind not in CHANGES_FILES:
        if t.state not in (State.DONE, State.NEEDS_DECISION):
            raise DecisionError(_t("accept.can_accept", label=t.label, state=t.state.value))
        transitions.move(store, t.id, State.ACCEPTED, reason=_t("accept.accepted"), by=by)
        events.ack_task(store, t.id)
        archive.write_task(store, project, t.id)
        workspace.remove(project, t.id, delete_branch=True)
        return _t("accept.accepted_msg", label=t.label)
    if t.state not in (State.DONE, State.NEEDS_DECISION, State.ACCEPTING):
        raise DecisionError(_t("accept.can_accept", label=t.label, state=t.state.value))
    if not t.worktree or not Path(t.worktree).is_dir():
        raise DecisionError(_t("accept.no_worktree", label=t.label, wt=t.worktree or "—"))
    _root_ready(project)
    owner = owner_token()
    if t.state is not State.ACCEPTING:
        transitions.move(store, t.id, State.ACCEPTING, reason=_t("accept.accepting"), by=by,
                         expect_from={State.DONE, State.NEEDS_DECISION})
    if not transitions.acquire(store, t.id, owner, pid=None):
        raise DecisionError(_t("accept.busy", label=t.label))
    try:
        return _merge(store, project, _get(store, t.id), owner, by)
    except DecisionError as e:
        _back(store, t.id, owner, str(e))
        raise
    except Exception as e:
        _log.exception("принятие T%d упало", t.id, extra={"task": t.id})
        _back(store, t.id, owner, _t("accept.fail", err=f"{type(e).__name__}: {e}"))
        raise DecisionError(_t("accept.fail", err=e)) from e
    finally:
        transitions.release(store, t.id, owner)


def _back(store: Store, task_id: int, owner: str, reason: str) -> None:
    t = store.get_task(task_id)
    if t is not None and t.state is State.ACCEPTING:
        transitions.move(store, task_id, State.NEEDS_DECISION, reason=reason[:500], by="accept", owner=owner)
        events.ack_task(store, task_id)  # отказ оркестратор увидел в выводе команды


def _merge(store: Store, project: ProjectConfig, t: Task, owner: str, by: str) -> str:
    res = archive.read_json(Path(t.worktree) / workspace.AHUB_DIR / "result.json")
    head = workspace.head(t.worktree)
    orch_edit = not res.get("commit") or not head.startswith(str(res.get("commit"))[:7])
    if orch_edit and not t.limits.get("orch_edit"):
        store.add_event(Ev.ORCH_EDIT, task_id=t.id, project=t.project,
                        payload={"head": head[:12], "worker_commit": str(res.get("commit", ""))[:12], "by": by})
    g = gates.check(project, t, run_tests=False, orch_edit=orch_edit)
    problems = g.fatal + [p for p in g.repairable if not (orch_edit and p.startswith("result.json"))]
    if problems:
        raise DecisionError(_t("accept.gates_head", problems="; ".join(problems)))
    title = t.title.replace('"', "'")[:100]
    r = workspace.git(project.root, "merge", "--no-ff", "-m", f"merge {t.label}: {title}", t.branch, check=False)
    if r.returncode != 0:
        conflicts = workspace.git(project.root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        workspace.git(project.root, "merge", "--abort", check=False)
        raise DecisionError(_t("accept.conflict", info=", ".join(conflicts[:10]) or (r.stderr or r.stdout)[-300:]))
    merged = workspace.git(project.root, "rev-parse", "HEAD").stdout.strip()
    nodes = list(t.limits.get("accept") or [])
    if t.kind is Kind.CODE and nodes:
        ok, tail, cmd = gates.run_acceptance(project, project.root, nodes, task_label=t.label)
        if not ok:
            head_now = workspace.git(project.root, "rev-parse", "HEAD").stdout.strip()
            if head_now != merged:  # в рабочую ветку успели закоммитить — чужое не трогаем
                raise DecisionError(_t("accept.root_moved", now=head_now[:10], merged=merged[:10]))
            workspace.git(project.root, "reset", "--keep", "HEAD~1", check=False)  # --keep не давит чужую грязь
            raise DecisionError(_t("accept.red_rolled_back", cmd=cmd, tail=tail[-600:]))
    note = ""
    if project.push.strip():
        parts = project.push.split()
        pr = workspace.git(project.root, "push", *parts, check=False, timeout=300)
        if pr.returncode != 0:
            note = _t("accept.push_fail", err=(pr.stderr or pr.stdout).strip()[-200:])
            _log.warning("push T%d: %s", t.id, note, extra={"task": t.id})
    transitions.move(store, t.id, State.ACCEPTED, reason=_t("accept.merged_reason", branch=project.work_branch,
                                                             note=note), by=by, owner=owner,
                     fields={"accepted_sha": merged})
    events.ack_task(store, t.id)
    try:
        prepare.run_hook(project, "task_cleanup", t, t.worktree)
    except prepare.PrepareError as e:
        _log.warning("хук task_cleanup T%d: %s", t.id, e, extra={"task": t.id})
    archive.write_task(store, project, t.id)
    workspace.remove(project, t.id, delete_branch=True)
    return _t("accept.merged_msg", label=t.label, branch=project.work_branch, sha=merged[:10], note=note)


def reject(store: Store, project: ProjectConfig, task_id: int, *, reason: str = "", by: str = "orchestrator",
           keep: bool = False) -> str:
    t = _get(store, task_id)
    try:
        transitions.move(store, t.id, State.REJECTED, reason=reason or _t("accept.rejected_default"), by=by)
    except (transitions.TransitionError, transitions.ConflictError) as e:
        raise DecisionError(f"{e}{_t('accept.active_first')}") from e
    events.ack_task(store, t.id)
    archive.write_task(store, project, t.id)
    if not keep:
        workspace.remove(project, t.id, delete_branch=True)
    return _t("accept.rejected_msg", label=t.label)


def rework(store: Store, task_id: int, notes: str, *, by: str = "orchestrator") -> str:
    """Вернуть на доработку с указаниями: та же сессия исполнителя, новый круг."""
    t = _get(store, task_id)
    if t.state not in (State.DONE, State.NEEDS_DECISION, State.ERROR, State.STOPPED):
        raise DecisionError(_t("accept.rework_state", label=t.label, state=t.state.value))
    if not notes.strip():
        raise DecisionError(_t("accept.need_notes"))
    lim = dict(t.limits)
    lim["rework_notes"] = notes.strip()
    store.update_task(t.id, limits=lim)
    transitions.move(store, t.id, State.QUEUED, reason=_t("accept.rework"), by=by, fields={"round": t.round + 1},
                     payload={"notes": notes[:500]})
    events.ack_task(store, t.id)
    return _t("accept.rework_msg", label=t.label, round=t.round + 1)


def continue_task(store: Store, task_id: int, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.state not in (State.STOPPED, State.ERROR, State.NEEDS_DECISION):
        raise DecisionError(_t("accept.continue_state", label=t.label, state=t.state.value))
    transitions.move(store, t.id, State.QUEUED, reason=_t("accept.continue"), by=by)
    events.ack_task(store, t.id)
    return _t("accept.continued_msg", label=t.label)


def edit(store: Store, project: ProjectConfig, task_id: int, *, spec: str | None = None,
         title: str | None = None, by: str = "orchestrator") -> str:
    """Новая постановка: при продолжении — новая сессия исполнителя (другой отпечаток)."""
    t = _get(store, task_id)
    if t.state in (State.ACCEPTED, State.REJECTED) or t.state in transitions.ACTIVE:
        raise DecisionError(_t("accept.edit_state", label=t.label))
    new = tasks.TaskSpec(project=t.project, kind=t.kind, title=title if title is not None else t.title,
                         spec=spec if spec is not None else t.spec, result_format=t.result_format,
                         paths=list(t.limits.get("paths") or []), accept=list(t.limits.get("accept") or []),
                         review_input=str(t.limits.get("input") or ""))
    h = tasks.spec_hash(new)
    if h == t.spec_hash:
        return _t("accept.edit_same", label=t.label)
    lim = dict(t.limits)
    lim["fresh_session"] = True
    store.update_task(t.id, title=new.title, spec=new.spec, spec_hash=h, limits=lim)
    store.add_event(Ev.STATE, task_id=t.id, project=t.project, payload={"edit": _t("accept.edit_text"), "by": by})
    return _t("accept.edit_msg", label=t.label)


def extend_paths(store: Store, project: ProjectConfig, task_id: int, paths: list[str], *,
                 by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    bad = [p for p in paths if tasks._path_escapes(p) or not tasks._glob_allowed(p, project.allowed_paths)]
    if bad:
        raise DecisionError(_t("accept.paths_outside", items=", ".join(bad)))
    cur = list(t.limits.get("paths") or [])
    added = [p for p in paths if p not in cur]
    if not added:
        return _t("accept.paths_same", label=t.label)
    lim = dict(t.limits)
    lim["paths"] = cur + added
    store.update_task(t.id, limits=lim)
    store.add_event(Ev.PATHS_EXTENDED, task_id=t.id, project=t.project, payload={"added": added, "by": by})
    return _t("accept.paths_added", label=t.label, items=", ".join(added))


def extend_budget(store: Store, task_id: int, *, add: float | None = None, set_to: float | None = None,
                  add_usd: float | None = None, by: str = "orchestrator") -> str:
    """Продлить бюджет одним действием: увеличен + (если задача стояла из-за бюджета) продолжена.

    add/set_to — счётчик Go (подписка); add_usd — реальные деньги (по умолчанию 0 = тратить нельзя).
    """
    t = _get(store, task_id)
    new = set_to if set_to is not None else t.budget_go + (add or 0.0)
    new_usd = t.budget_usd + (add_usd or 0.0)
    if new <= t.budget_go and set_to is None and new_usd <= t.budget_usd:
        raise DecisionError(_t("accept.budget_need"))
    store.update_task(t.id, budget_go=float(new), budget_usd=float(new_usd))
    store.add_event(Ev.BUDGET_EXTENDED, task_id=t.id, project=t.project,
                    payload={"from": t.budget_go, "to": new, "usd_from": t.budget_usd, "usd_to": new_usd, "by": by})
    msg = _t("accept.budget_msg", label=t.label, old=f"{t.budget_go:g}", new=f"{new:g}") + (
        _t("accept.budget_usd", old=f"{t.budget_usd:g}", new=f"{new_usd:g}")
        if new_usd != t.budget_usd else "")
    if t.state is State.NEEDS_DECISION and t.state_reason.startswith(("бюджет", "budget")):
        transitions.move(store, t.id, State.QUEUED, reason=_t("accept.budget_long"), by=by)
        events.ack_task(store, t.id)
        msg += _t("accept.budget_resumed")
    return msg


def change_model(store: Store, project: ProjectConfig, task_id: int, alias: str, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.state in transitions.ACTIVE:
        raise DecisionError(_t("accept.model_active", label=t.label))
    try:
        registry.check(store, alias, project)
    except registry.RegistryError as e:
        raise DecisionError(str(e)) from e
    lim = dict(t.limits)
    lim["fresh_session"] = True  # сессию другой модели не продолжить
    store.update_task(t.id, executor=alias, limits=lim)
    store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project, payload={"from": t.executor, "to": alias, "by": by})
    return _t("accept.model_msg", label=t.label, old=t.executor, new=alias)
