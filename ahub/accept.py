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
from ahub.model import CHANGES_FILES, Ev, Kind, State
from ahub.store import Store, Task

_log = hublog.get("accept")


class DecisionError(RuntimeError):
    """Действие невозможно (одна строка для оркестратора)."""


def _get(store: Store, task_id: int) -> Task:
    t = store.get_task(task_id)
    if t is None:
        raise DecisionError(f"нет задачи T{task_id}")
    return t


def _root_ready(project: ProjectConfig) -> None:
    cur = workspace.git(project.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != project.work_branch:
        raise DecisionError(f"корень проекта на ветке {cur}, а слияние — в {project.work_branch}")
    dirty = [ln for ln in workspace.git(project.root, "status", "--porcelain", "--untracked-files=no").stdout
             .splitlines() if ln.strip()]
    if dirty:
        raise DecisionError("в корне проекта незакоммиченные изменения: " + ", ".join(x[3:] for x in dirty[:5]))


def accept(store: Store, project: ProjectConfig, task_id: int, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.kind not in CHANGES_FILES:
        if t.state not in (State.DONE, State.NEEDS_DECISION):
            raise DecisionError(f"{t.label}: принять можно «готово/нужно решение», сейчас {t.state.value}")
        transitions.move(store, t.id, State.ACCEPTED, reason="принята", by=by)
        events.ack_task(store, t.id)
        archive.write_task(store, project, t.id)
        workspace.remove(project, t.id, delete_branch=True)
        return f"{t.label} принята"
    if t.state not in (State.DONE, State.NEEDS_DECISION, State.ACCEPTING):
        raise DecisionError(f"{t.label}: принять можно «готово/нужно решение», сейчас {t.state.value}")
    if not t.worktree or not Path(t.worktree).is_dir():
        raise DecisionError(f"{t.label}: нет копии задачи ({t.worktree or '—'})")
    _root_ready(project)
    owner = owner_token()
    if t.state is not State.ACCEPTING:
        transitions.move(store, t.id, State.ACCEPTING, reason="принятие", by=by,
                         expect_from={State.DONE, State.NEEDS_DECISION})
    if not transitions.acquire(store, t.id, owner, pid=None):
        raise DecisionError(f"{t.label}: уже принимается другим процессом")
    try:
        return _merge(store, project, _get(store, t.id), owner, by)
    except DecisionError as e:
        _back(store, t.id, owner, str(e))
        raise
    except Exception as e:
        _log.exception("принятие T%d упало", t.id, extra={"task": t.id})
        _back(store, t.id, owner, f"сбой принятия: {type(e).__name__}: {e}")
        raise DecisionError(f"сбой принятия: {e}") from e
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
        raise DecisionError("ворота на текущем HEAD: " + "; ".join(problems))
    title = t.title.replace('"', "'")[:100]
    r = workspace.git(project.root, "merge", "--no-ff", "-m", f"merge {t.label}: {title}", t.branch, check=False)
    if r.returncode != 0:
        conflicts = workspace.git(project.root, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
        workspace.git(project.root, "merge", "--abort", check=False)
        raise DecisionError("конфликт слияния: " + (", ".join(conflicts[:10]) or (r.stderr or r.stdout)[-300:]))
    merged = workspace.git(project.root, "rev-parse", "HEAD").stdout.strip()
    nodes = list(t.limits.get("accept") or [])
    if t.kind is Kind.CODE and nodes:
        ok, tail, cmd = gates.run_acceptance(project, project.root, nodes, task_label=t.label)
        if not ok:
            workspace.git(project.root, "reset", "--hard", "HEAD~1", check=False)
            raise DecisionError(f"после слияния приёмка красная — слияние откачено ({cmd}):\n{tail[-600:]}")
    note = ""
    if project.push.strip():
        parts = project.push.split()
        pr = workspace.git(project.root, "push", *parts, check=False, timeout=300)
        if pr.returncode != 0:
            note = f"; push не прошёл: {(pr.stderr or pr.stdout).strip()[-200:]}"
            _log.warning("push T%d: %s", t.id, note, extra={"task": t.id})
    transitions.move(store, t.id, State.ACCEPTED, reason=f"слита в {project.work_branch}{note}", by=by, owner=owner,
                     fields={"accepted_sha": merged})
    events.ack_task(store, t.id)
    try:
        prepare.run_hook(project, "task_cleanup", t, t.worktree)
    except prepare.PrepareError as e:
        _log.warning("хук task_cleanup T%d: %s", t.id, e, extra={"task": t.id})
    archive.write_task(store, project, t.id)
    workspace.remove(project, t.id, delete_branch=True)
    return f"{t.label} слита в {project.work_branch} ({merged[:10]}){note}"


def reject(store: Store, project: ProjectConfig, task_id: int, *, reason: str = "", by: str = "orchestrator",
           keep: bool = False) -> str:
    t = _get(store, task_id)
    try:
        transitions.move(store, t.id, State.REJECTED, reason=reason or "отклонена", by=by)
    except (transitions.TransitionError, transitions.ConflictError) as e:
        raise DecisionError(f"{e} (активную задачу сначала остановите)") from e
    events.ack_task(store, t.id)
    archive.write_task(store, project, t.id)
    if not keep:
        workspace.remove(project, t.id, delete_branch=True)
    return f"{t.label} отклонена"


def rework(store: Store, task_id: int, notes: str, *, by: str = "orchestrator") -> str:
    """Вернуть на доработку с указаниями: та же сессия исполнителя, новый круг."""
    t = _get(store, task_id)
    if t.state not in (State.DONE, State.NEEDS_DECISION, State.ERROR, State.STOPPED):
        raise DecisionError(f"{t.label}: доработка из «готово/нужно решение/ошибка/остановлена», сейчас {t.state.value}")
    if not notes.strip():
        raise DecisionError("нужны указания (--notes)")
    lim = dict(t.limits)
    lim["rework_notes"] = notes.strip()
    store.update_task(t.id, limits=lim)
    transitions.move(store, t.id, State.QUEUED, reason="доработка", by=by, fields={"round": t.round + 1},
                     payload={"notes": notes[:500]})
    events.ack_task(store, t.id)
    return f"{t.label} на доработке (круг {t.round + 1})"


def continue_task(store: Store, task_id: int, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.state not in (State.STOPPED, State.ERROR, State.NEEDS_DECISION):
        raise DecisionError(f"{t.label}: продолжить можно из «остановлена/ошибка/нужно решение», сейчас {t.state.value}")
    transitions.move(store, t.id, State.QUEUED, reason="продолжить", by=by)
    events.ack_task(store, t.id)
    return f"{t.label} снова в очереди"


def edit(store: Store, project: ProjectConfig, task_id: int, *, spec: str | None = None,
         title: str | None = None, by: str = "orchestrator") -> str:
    """Новая постановка: при продолжении — новая сессия исполнителя (другой отпечаток)."""
    t = _get(store, task_id)
    if t.state in (State.ACCEPTED, State.REJECTED) or t.state in transitions.ACTIVE:
        raise DecisionError(f"{t.label}: менять постановку можно у неактивной незавершённой задачи")
    new = tasks.TaskSpec(project=t.project, kind=t.kind, title=title if title is not None else t.title,
                         spec=spec if spec is not None else t.spec, result_format=t.result_format,
                         paths=list(t.limits.get("paths") or []), accept=list(t.limits.get("accept") or []),
                         review_input=str(t.limits.get("input") or ""))
    h = tasks.spec_hash(new)
    if h == t.spec_hash:
        return f"{t.label}: постановка не изменилась"
    lim = dict(t.limits)
    lim["fresh_session"] = True
    store.update_task(t.id, title=new.title, spec=new.spec, spec_hash=h, limits=lim)
    store.add_event(Ev.STATE, task_id=t.id, project=t.project, payload={"edit": "постановка изменена", "by": by})
    return f"{t.label}: постановка обновлена — при продолжении новая сессия"


def extend_paths(store: Store, project: ProjectConfig, task_id: int, paths: list[str], *,
                 by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    bad = [p for p in paths if tasks._path_escapes(p) or not tasks._glob_allowed(p, project.allowed_paths)]
    if bad:
        raise DecisionError(f"вне разрешённых проекту: {', '.join(bad)}")
    cur = list(t.limits.get("paths") or [])
    added = [p for p in paths if p not in cur]
    if not added:
        return f"{t.label}: файлы уже разрешены"
    lim = dict(t.limits)
    lim["paths"] = cur + added
    store.update_task(t.id, limits=lim)
    store.add_event(Ev.PATHS_EXTENDED, task_id=t.id, project=t.project, payload={"added": added, "by": by})
    return f"{t.label}: разрешены ещё {', '.join(added)}"


def extend_budget(store: Store, task_id: int, *, add: float | None = None, set_to: float | None = None,
                  by: str = "orchestrator") -> str:
    """Продлить бюджет одним действием: увеличен + (если задача стояла из-за бюджета) продолжена."""
    t = _get(store, task_id)
    new = set_to if set_to is not None else t.budget_go + (add or 0.0)
    if new <= t.budget_go and set_to is None:
        raise DecisionError("нужно --add > 0 или --set")
    store.update_task(t.id, budget_go=float(new))
    store.add_event(Ev.BUDGET_EXTENDED, task_id=t.id, project=t.project,
                    payload={"from": t.budget_go, "to": new, "by": by})
    msg = f"{t.label}: бюджет ${t.budget_go:g} → ${new:g}"
    if t.state is State.NEEDS_DECISION and t.state_reason.startswith("бюджет"):
        transitions.move(store, t.id, State.QUEUED, reason="бюджет продлён", by=by)
        events.ack_task(store, t.id)
        msg += ", задача продолжена"
    return msg


def change_model(store: Store, project: ProjectConfig, task_id: int, alias: str, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.state in transitions.ACTIVE:
        raise DecisionError(f"{t.label}: модель меняется у неактивной задачи (сначала остановите)")
    try:
        registry.check(store, alias, project)
    except registry.RegistryError as e:
        raise DecisionError(str(e)) from e
    lim = dict(t.limits)
    lim["fresh_session"] = True  # сессию другой модели не продолжить
    store.update_task(t.id, executor=alias, limits=lim)
    store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project, payload={"from": t.executor, "to": alias, "by": by})
    return f"{t.label}: модель {t.executor} → {alias}"
