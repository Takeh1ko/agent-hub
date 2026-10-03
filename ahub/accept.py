"""Decisions on results (V15–V18, architecture §6.7–§6.8): accept/merge, rework, reject, orchestrator edit,
path extension, budget top-up, model change, resume with a new brief.

Merge: task → "accepting" (lease held by whoever accepts; a second "accept" is refused) → gates at the copy's
current HEAD (orchestrator edit — if HEAD ≠ worker result commit: legitimate, with an event) → in the project root
`git merge --no-ff` into the work branch → acceptance under the test resource → red — roll the merge back →
push per config → "accepted", task_cleanup hook, copy and branch removed, archived.

Resumable: if the task branch is already merged into the work branch (the accept was interrupted after the merge
— the gates there see nothing but the merge commit), the gates and the merge are skipped: acceptance runs on HEAD
(red — roll the merge back, as usual), then the same tail.
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
    """Action impossible (one line for the orchestrator)."""


def _get(store: Store, task_id: int) -> Task:
    t = store.get_task(task_id)
    if t is None:
        raise DecisionError(_t("trans.no_task", id=task_id))
    return t


def merged_sha(project: ProjectConfig, t: Task) -> str:
    """The work branch HEAD when the task branch tip is already in it ('' — not merged yet).

    That is the state of an accept interrupted after the merge: the gates on the copy see an empty diff.
    """
    if not t.branch:
        return ""
    r = workspace.git(project.root, "merge-base", "--is-ancestor", t.branch, project.work_branch, check=False)
    if r.returncode != 0:
        return ""
    return workspace.git(project.root, "rev-parse", project.work_branch).stdout.strip()


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
    merged = merged_sha(project, t)  # an interrupted accept: the merge is already in the work branch
    if not merged and (not t.worktree or not Path(t.worktree).is_dir()):
        raise DecisionError(_t("accept.no_worktree", label=t.label, wt=t.worktree or "—"))
    _root_ready(project)
    owner = owner_token()
    if t.state is not State.ACCEPTING:
        transitions.move(store, t.id, State.ACCEPTING, reason=_t("accept.accepting"), by=by,
                         expect_from={State.DONE, State.NEEDS_DECISION})
    if not transitions.acquire(store, t.id, owner, pid=None):
        raise DecisionError(_t("accept.busy", label=t.label))
    try:
        return _merge(store, project, _get(store, t.id), owner, by, merged=merged)
    except DecisionError as e:
        _back(store, t.id, owner, str(e))
        raise
    except Exception as e:
        _log.exception("accept T%d failed", t.id, extra={"task": t.id})
        _back(store, t.id, owner, _t("accept.fail", err=f"{type(e).__name__}: {e}"))
        raise DecisionError(_t("accept.fail", err=e)) from e
    finally:
        transitions.release(store, t.id, owner)


def _back(store: Store, task_id: int, owner: str, reason: str) -> None:
    t = store.get_task(task_id)
    if t is not None and t.state is State.ACCEPTING:
        transitions.move(store, task_id, State.NEEDS_DECISION, reason=reason[:500], by="accept", owner=owner)
        events.ack_task(store, task_id)  # the orchestrator saw the refusal in the command output


def _merge(store: Store, project: ProjectConfig, t: Task, owner: str, by: str, *, merged: str = "") -> str:
    if not merged:  # an empty merged — the branch is not in the work branch yet: gates on the copy, then merge
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
    else:
        _log.info("T%d is already merged into %s (%s) — acceptance on HEAD", t.id, project.work_branch, merged[:10])
    nodes = list(t.limits.get("accept") or [])
    if t.kind is Kind.CODE and nodes:
        ok, tail, cmd = gates.run_acceptance(project, project.root, nodes, task_label=t.label)
        if not ok:
            head_now = workspace.git(project.root, "rev-parse", "HEAD").stdout.strip()
            if head_now != merged:  # someone committed into the work branch meanwhile — leave foreign commits alone
                raise DecisionError(_t("accept.root_moved", now=head_now[:10], merged=merged[:10]))
            workspace.git(project.root, "reset", "--keep", "HEAD~1", check=False)  # --keep leaves foreign dirt alone
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
        _log.warning("hook task_cleanup T%d: %s", t.id, e, extra={"task": t.id})
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
    """Send back for rework with notes: same executor session, new round."""
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
    """New brief: on resume — a fresh executor session (different fingerprint)."""
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


def _stopped_by_budget(store: Store, task_id: int) -> bool:
    """Task parked on budget: after a hard budget stop (budget_hard) it never left "needs decision"."""
    stopped = False
    for e in store.events(task_id=task_id):
        if e.kind == Ev.BUDGET_HARD.value:
            stopped = True
        elif e.kind == Ev.STATE.value and (e.payload or {}).get("to") != State.NEEDS_DECISION.value:
            stopped = False
    return stopped


def extend_budget(store: Store, task_id: int, *, add: float | None = None, set_to: float | None = None,
                  add_usd: float | None = None, by: str = "orchestrator") -> str:
    """Top up the budget in one move: raised + resumed (if the task was parked on budget).

    add/set_to — Go counter (subscription); add_usd — real money (default 0 = no spending).
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
    if t.state is State.NEEDS_DECISION and _stopped_by_budget(store, t.id):
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
    lim["fresh_session"] = True  # never resume another model's session
    store.update_task(t.id, executor=alias, limits=lim)
    store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project, payload={"from": t.executor, "to": alias, "by": by})
    return _t("accept.model_msg", label=t.label, old=t.executor, new=alias)
