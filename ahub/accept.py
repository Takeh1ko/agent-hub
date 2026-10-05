"""Decisions on results (V15–V18, architecture §6.7–§6.8): accept/merge, rework, reject, orchestrator edit,
path extension, budget top-up, model change, resume with a new brief.

Merge: task → "accepting" (lease held by whoever accepts; a second "accept" is refused) → a per-project flock
(the whole sequence below is one at a time per project, a second accept waits) → gates at the copy's
current HEAD (orchestrator edit — if HEAD ≠ worker result commit: legitimate, with an event) → merge
`git merge --no-ff` into a temporary worktree/branch from the work-branch tip → acceptance there under
the test resource (the accept waits for the test lock ahead of task gates) → green — fast-forward the
work branch to the verified merge (the branch moved meanwhile — refuse); red — nothing ever merged →
push per config → "accepted", task_cleanup hook, copy and branch removed, archived. The temp worktree
is removed in every outcome.

Resumable: an interrupted accept leaves only the temp worktree to clean (it is removed at the next
start and at the end); if accept's own merge commit (`merge T<n>: …`, second parent = the task branch
tip) is already the tip of the work branch (the accept was interrupted after the fast-forward), the gates
and the merge are skipped: acceptance runs on HEAD (red — roll the merge back, as usual), then the same
tail. A foreign merge is never taken for that state: a hand-merge, or an extra commit on top of it, is
refused for the orchestrator to finish by hand (`already_merged`).

The lease is taken with this process's pid and renewed in the background while the acceptance runs: a long
acceptance is not an orphan, and `service` leaves an "accepting" task with a live owner process alone.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ahub import archive, events, gates, paths, prepare, reasons, registry, tasks, transitions, workspace
from ahub import log as hublog
from ahub.config import ProjectConfig
from ahub.engine import owner_token
from ahub.i18n import t as _t
from ahub.model import CHANGES_FILES, Ev, Kind, State
from ahub.store import Store, Task
from ahub.time import now_ms

_log = hublog.get("accept")

ACCEPT_LEASE_MS = transitions.DEFAULT_LEASE_MS  # the accept process holds the lease while it runs
RENEW_S = 20.0  # how often the lease is renewed during acceptance (minutes-long runs on a big repo)
LOCK_POLL_S = 0.2  # how often a waiting accept retries the project accept lock


class DecisionError(RuntimeError):
    """Action impossible (one line for the orchestrator).

    `reason` — the stored form (a reason code blob); empty — the message itself goes on the task.
    `hint` — the command that gets out of it (the CLI prints it under the error).
    """

    def __init__(self, message: str, reason: str = "", hint: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.hint = hint


def _expire_quota_hold(limits: dict) -> dict:
    """Explicit owner input beats the quota backoff: the task wakes at the next tick.

    Only the deferral expires (not_before → 0); the resume info stays, so a re-pick with the
    work still delivered resumes at the review instead of redoing it.
    """
    hold = limits.get("quota_hold")
    if isinstance(hold, dict) and hold.get("not_before"):
        limits = dict(limits)
        limits["quota_hold"] = {**hold, "not_before": 0}
        return limits
    return limits


def _clear_loop(limits: dict) -> dict:
    """Owner acted (continue/rework/edit): a loop/stuck episode is over, the task may spin again."""
    if "loop" in limits or "stuck" in limits:
        limits = dict(limits)
        limits.pop("loop", None)
        limits.pop("stuck", None)
    return limits


def _get(store: Store, task_id: int) -> Task:
    t = store.get_task(task_id)
    if t is None:
        raise DecisionError(_t("trans.no_task", id=task_id))
    return t


def _merged_sha(project: ProjectConfig, t: Task) -> str:
    """The work branch HEAD when the task branch tip is already in it ('' — not merged yet).

    That is the state of an accept interrupted after the merge: the gates on the copy see an empty diff.
    Only accept's own merge commit at the tip of the work branch qualifies.
    """
    if not t.branch:
        return ""
    r = workspace.git(project.root, "merge-base", "--is-ancestor", t.branch, project.work_branch, check=False)
    if r.returncode != 0:
        return ""
    head = workspace.git(project.root, "rev-parse", project.work_branch, check=False).stdout.strip()
    if not head:
        return ""
    parents = workspace.git(project.root, "rev-parse", f"{head}^@", check=False).stdout.split()
    if len(parents) < 2:
        return ""
    branch_tip = workspace.git(project.root, "rev-parse", t.branch, check=False).stdout.strip()
    if parents[1] != branch_tip:
        return ""
    subj = workspace.git(project.root, "log", "-1", "--format=%s", head, check=False).stdout.strip()
    if not subj.startswith(f"merge {t.label}:"):
        return ""
    if t.worktree and Path(t.worktree).is_dir():
        wt_head = workspace.head(t.worktree)
        if wt_head and branch_tip != wt_head:
            return ""
    return head


def _root_ready(project: ProjectConfig) -> None:
    cur = workspace.git(project.root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if cur != project.work_branch:
        raise DecisionError(_t("accept.root_branch", cur=cur, branch=project.work_branch))
    dirty = [ln for ln in workspace.git(project.root, "status", "--porcelain", "--untracked-files=no").stdout
             .splitlines() if ln.strip()]
    if dirty:
        raise DecisionError(_t("accept.root_dirty", files=", ".join(x[3:] for x in dirty[:5])))


def _verify_branch(project: ProjectConfig, t: Task) -> str:
    """Temp branch for the verified merge (one per task; the per-project accept lock makes it unique)."""
    return f"{project.branch_prefix}accept-{t.label}"


def _verify_path(project: ProjectConfig, t: Task) -> Path:
    """Temp worktree for the verified merge, next to the task copies."""
    base = Path(project.worktrees) if project.worktrees else Path(project.root).parent / f"{Path(project.root).name}-wt"
    return base / f"accept-{t.label}"


def _clean_verify(project: ProjectConfig, t: Task) -> None:
    """Remove the temp worktree/branch (an interrupted accept leaves only this)."""
    path = _verify_path(project, t)
    if path.exists():
        workspace.git(project.root, "worktree", "remove", "--force", str(path), check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    workspace.git(project.root, "worktree", "prune", check=False)
    workspace.git(project.root, "branch", "-D", _verify_branch(project, t), check=False)


def _is_tty() -> bool:
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):  # a replaced or closed stdout
        return False


def _holder_label(fd: int, tries: int = 1) -> str:
    """The label the lock holder wrote in ('' — it has just taken the lock, or the write has not landed).

    The flock and the label are two steps, so a waiter that reads in between sees an empty file — hence `tries`.
    """
    for _ in range(tries):
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 200).decode("utf-8", errors="replace").strip()
        except OSError:
            raw = ""
        if raw:
            return raw.splitlines()[0]
        time.sleep(0.05)  # the holder writes it within microseconds of the flock
    return ""


@contextmanager
def _project_lock(project: ProjectConfig, t: Task) -> Iterator[None]:
    """One accept at a time per project: the whole verify -> fast-forward-or-push under an flock.

    Two accepts of one project interleaved once: the second merge moved the root under a running acceptance, the red
    one could no longer roll its merge back ("acceptance is red after the merge, but the root moved"), and both
    merges went to the branch — hence the verify-before-move below. The lock file is in the hub data dir, keyed
    by the project name; the holder writes its label in, so a waiter can name it. A dead holder releases the flock
    with its fd — nobody waits forever.
    """
    lock_file = paths.accept_lock_path(project.name)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o666)
    try:
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not waited:
                    waited = True
                    _log.info("accept T%d: waiting for the project accept lock", t.id, extra={"task": t.id})
                    if _is_tty():  # one line on a terminal; in a pipe the accept output is unchanged
                        sys.stdout.write(_t("accept.waiting", label=_holder_label(fd, tries=10)) + "\n")
                        sys.stdout.flush()
                time.sleep(LOCK_POLL_S)
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, f"{t.label}\n".encode("utf-8"))
            yield
        finally:
            os.ftruncate(fd, 0)  # the holder is named only while it holds the lock
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def accept(store: Store, project: ProjectConfig, task_id: int, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.kind not in CHANGES_FILES:
        if t.state not in (State.DONE, State.NEEDS_DECISION):
            raise DecisionError(_t("accept.can_accept", label=t.label, state=t.state.value))
        transitions.move(store, t.id, State.ACCEPTED, reason=reasons.dump("accepted"), by=by)
        events.ack_task(store, t.id)
        archive.write_task(store, project, t.id)
        workspace.remove(project, t.id, delete_branch=True)
        return _t("accept.accepted_msg", label=t.label)
    if t.state not in (State.DONE, State.NEEDS_DECISION, State.ACCEPTING):
        raise DecisionError(_t("accept.can_accept", label=t.label, state=t.state.value))
    try:
        merged = _merged_sha(project, t)  # an interrupted accept: the merge is already in the work branch
    except workspace.WorkspaceError as e:  # the path is a directory, but not a git worktree
        raise DecisionError(_t("accept.no_worktree", label=t.label, wt=t.worktree or "—")) from e
    if not merged and (not t.worktree or not Path(t.worktree).is_dir()):
        raise DecisionError(_t("accept.no_worktree", label=t.label, wt=t.worktree or "—"))
    _root_ready(project)
    owner = owner_token()
    if t.state is not State.ACCEPTING:
        transitions.move(store, t.id, State.ACCEPTING, reason=reasons.dump("accepting"), by=by,
                         expect_from={State.DONE, State.NEEDS_DECISION})
    if not transitions.acquire(store, t.id, owner, pid=os.getpid(), lease_ms=ACCEPT_LEASE_MS):
        raise DecisionError(_t("accept.busy", label=t.label))
    try:
        with transitions.keep_lease(store, t.id, owner, lease_ms=ACCEPT_LEASE_MS, interval_s=RENEW_S,
                                    name=f"accept-lease-T{t.id}"):
            # the lease keeper wraps the lock: a long wait for it is not an expired lease either
            with _project_lock(project, t):
                _root_ready(project)  # the root may have moved or got dirty while we were waiting for the lock
                merged = _merged_sha(project, t)  # so may the branch: an interrupted accept is re-read under the lock
                return _merge(store, project, _get(store, t.id), owner, by, merged=merged)
    except DecisionError as e:
        _back(store, t.id, owner, e.reason or str(e))
        raise
    except Exception as e:
        _log.exception("accept T%d failed", t.id, extra={"task": t.id})
        detail = f"{type(e).__name__}: {e}"
        _back(store, t.id, owner, reasons.dump("accept_failed", err=detail))
        raise DecisionError(_t("accept.fail", err=e)) from e
    finally:
        transitions.release(store, t.id, owner)


def _back(store: Store, task_id: int, owner: str, reason: str) -> None:
    t = store.get_task(task_id)
    if t is not None and t.state is State.ACCEPTING:
        stored = reason if reasons.load(reason) else reason[:500]
        transitions.move(store, task_id, State.NEEDS_DECISION, reason=stored, by="accept", owner=owner)
        events.ack_task(store, task_id)  # the orchestrator saw the refusal in the command output


def _verify_merge(project: ProjectConfig, t: Task, title: str) -> str:
    """Merge into a temp worktree, run acceptance there, fast-forward the work branch. Returns its tip.

    Main never shows an unverified merge: the merge commit (`merge T<n>: …`) is built on a temp branch
    from the work-branch tip and tested there; only green moves the work branch (fast-forward). Red —
    nothing on the work branch ever changed. The temp worktree is removed in every outcome.
    """
    base_tip = workspace.git(project.root, "rev-parse", project.work_branch).stdout.strip()
    tmp_path = _verify_path(project, t)
    tmp_branch = _verify_branch(project, t)
    _clean_verify(project, t)
    try:
        workspace.git(project.root, "worktree", "add", "-b", tmp_branch, str(tmp_path), base_tip)
        r = workspace.git(str(tmp_path), "merge", "--no-ff", "-m", f"merge {t.label}: {title}", t.branch,
                          check=False)
        if r.returncode != 0:
            diff_u = workspace.git(str(tmp_path), "diff", "--name-only", "--diff-filter=U",
                                   check=False).stdout.split()
            workspace.git(str(tmp_path), "merge", "--abort", check=False)
            conflicts = ", ".join(diff_u[:10]) or (r.stderr or r.stdout)[-300:]
            raise DecisionError(_t("accept.conflict", info=conflicts), reasons.dump("merge_conflict", files=conflicts))
        verified = workspace.git(str(tmp_path), "rev-parse", "HEAD").stdout.strip()
        if verified == base_tip:
            raise DecisionError(_t("accept.already_merged", branch=project.work_branch, label=t.label),
                                reasons.dump("already_merged", branch=project.work_branch, label=t.label))
        nodes = list(t.limits.get("accept") or [])
        if t.kind is Kind.CODE and nodes:
            try:
                ok, tail, cmd = gates.run_acceptance(project, str(tmp_path), nodes, task_label=t.label,
                                                     is_accept=True)
            except gates.LockTimeout:
                # a busy test lock is a wait, not a red acceptance: nothing merged, retry the accept
                raise DecisionError(_t("accept.test_lock_busy"),
                                    reasons.dump("wait_test_lock")) from None
            if not ok:
                raise DecisionError(_t("accept.red", cmd=cmd, tail=tail[-600:]),
                                    reasons.dump("accept_red", cmd=cmd))
        head_now = workspace.git(project.root, "rev-parse", project.work_branch, check=False).stdout.strip()
        if head_now != base_tip:  # the work branch moved under the running acceptance — leave it alone
            raise DecisionError(_t("accept.work_moved", now=head_now[:10], base=base_tip[:10]),
                                reasons.dump("work_moved", now=head_now[:10], base=base_tip[:10]))
        _root_ready(project)  # the root may have got dirty while the acceptance ran
        fr = workspace.git(project.root, "merge", "--ff-only", verified, check=False)
        if fr.returncode != 0:
            head_now2 = workspace.git(project.root, "rev-parse", project.work_branch, check=False).stdout.strip()
            if head_now2 != base_tip:
                raise DecisionError(_t("accept.work_moved", now=head_now2[:10], base=base_tip[:10]),
                                    reasons.dump("work_moved", now=head_now2[:10], base=base_tip[:10]))
            err = (fr.stderr or fr.stdout).strip()[-300:] or "fast-forward failed"
            raise DecisionError(_t("accept.fail", err=err), reasons.dump("accept_failed", err=err))
        return verified
    finally:
        _clean_verify(project, t)


def _merge(store: Store, project: ProjectConfig, t: Task, owner: str, by: str, *, merged: str = "") -> str:
    fresh = not merged
    if not merged:  # an empty merged — the branch is not in the work branch yet: gates on the copy, then verify
        res = archive.read_json(Path(t.worktree) / workspace.AHUB_DIR / "result.json")
        head = workspace.head(t.worktree)
        orch_edit = not res.get("commit") or not head.startswith(str(res.get("commit"))[:7])
        if orch_edit and not t.limits.get("orch_edit"):
            store.add_event(Ev.ORCH_EDIT, task_id=t.id, project=t.project,
                            payload={"head": head[:12], "worker_commit": str(res.get("commit", ""))[:12], "by": by})
        g = gates.check(project, t, run_tests=False, orch_edit=orch_edit)
        # a result.json problem after an orchestrator edit is expected — decide by the problem code, not by
        # its text (the text is translated)
        problems = g.fatal + [p for p in g.repairable
                              if not (orch_edit and isinstance(p, gates.Problem)
                                      and p.code in gates.RESULT_JSON_CODES)]
        if problems:
            raise DecisionError(_t("accept.gates_head", problems="; ".join(problems)),
                                reasons.dump("accept_gates", problems=gates.codes(problems)))
        title = t.title.replace('"', "'")[:100]
        merged = _verify_merge(project, t, title)
    else:
        _clean_verify(project, t)  # a leftover temp worktree from the interrupted run
        _log.info("T%d is already merged into %s (%s) — acceptance on HEAD", t.id, project.work_branch, merged[:10])
    nodes = list(t.limits.get("accept") or [])
    if not fresh and t.kind is Kind.CODE and nodes:
        # an interrupted accept whose merge is already the tip: acceptance on HEAD (rollback if red)
        try:
            ok, tail, cmd = gates.run_acceptance(project, project.root, nodes, task_label=t.label, is_accept=True)
        except gates.LockTimeout:
            # a busy test lock is a wait, not a red acceptance: no rollback, retry the accept
            raise DecisionError(_t("accept.test_lock_busy"),
                                reasons.dump("wait_test_lock")) from None
        if not ok:
            head_now = workspace.git(project.root, "rev-parse", "HEAD").stdout.strip()
            if head_now != merged:  # someone committed into the work branch meanwhile — leave foreign commits alone
                raise DecisionError(_t("accept.root_moved", now=head_now[:10], merged=merged[:10]),
                                    reasons.dump("root_moved", now=head_now[:10], merged=merged[:10]))
            # --keep leaves foreign dirt alone; it refuses rather than overwrite it
            rr = workspace.git(project.root, "reset", "--keep", "HEAD~1", check=False)
            head_after_reset = workspace.git(project.root, "rev-parse", "HEAD").stdout.strip()
            if rr.returncode != 0 or head_after_reset == merged:
                err = (rr.stderr or rr.stdout).strip()[-300:] or "reset did not move HEAD"
                raise DecisionError(_t("accept.rollback_failed", err=err),
                                    reasons.dump("rollback_failed", err=err))
            raise DecisionError(_t("accept.red_rolled_back", cmd=cmd, tail=tail[-600:]),
                                reasons.dump("red_rolled_back", cmd=cmd))
    push_error = ""
    if project.push.strip():
        parts = project.push.split()
        pr = workspace.git(project.root, "push", *parts, check=False, timeout=300)
        if pr.returncode != 0:
            push_error = (pr.stderr or pr.stdout).strip()[-200:]
            _log.warning("push T%d: %s", t.id, push_error, extra={"task": t.id})
    note = _t("accept.push_fail", err=push_error) if push_error else ""
    transitions.move(store, t.id, State.ACCEPTED,
                     reason=reasons.dump("merged", branch=project.work_branch,
                                         note=reasons.part("push_failed", err=push_error) if push_error else ""),
                     by=by, owner=owner, fields={"accepted_sha": merged})
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
        transitions.move(store, t.id, State.REJECTED, reason=reason or reasons.dump("rejected"), by=by)
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
    store.update_task(t.id, limits=_clear_loop(_expire_quota_hold(lim)))
    transitions.move(store, t.id, State.QUEUED, reason=reasons.dump("rework"), by=by, fields={"round": t.round + 1},
                     payload={"notes": notes[:500]})
    events.ack_task(store, t.id)
    return _t("accept.rework_msg", label=t.label, round=t.round + 1)


def continue_task(store: Store, task_id: int, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.state not in (State.STOPPED, State.ERROR, State.NEEDS_DECISION):
        raise DecisionError(_t("accept.continue_state", label=t.label, state=t.state.value))
    lim = _clear_loop(_expire_quota_hold(dict(t.limits)))
    transitions.move(store, t.id, State.QUEUED, reason=reasons.dump("continue_task"), by=by,
                     fields={"limits": lim} if lim != t.limits else None)
    events.ack_task(store, t.id)
    return _t("accept.continued_msg", label=t.label)


REVIEW_PHASES = frozenset({State.CHECKING, State.REVIEWING, State.FIXING})  # the review is past


def review_started(store: Store, task_id: int) -> bool:
    """Has the review begun? Only a STATE event carries a state in "to" (MODEL_CHANGED writes a model
    alias there too), so the history of the state events is what answers this."""
    phases = {s.value for s in REVIEW_PHASES}
    return any(e.kind == Ev.STATE.value and str((e.payload or {}).get("to") or "") in phases
               for e in store.events(task_id=task_id))


def _panel_locked(store: Store, t: Task, flag: str) -> None:
    """A review task runs its panel (engine._review) — the executor is only the fallback reviewer, so
    every way of naming a reviewer (`--review`, `--model`, `ahub model`) goes through this lock."""
    if review_started(store, t.id):
        raise DecisionError(_t("accept.review_panel_locked", label=t.label, flag=flag),
                            hint=_t("hint.status_task", label=t.label))


def edit(store: Store, project: ProjectConfig, task_id: int, *, spec: str | None = None,
         title: str | None = None, review: list[str] | None = None, rounds: int | None = None,
         model: str | None = None, input: str | None = None, by: str = "orchestrator") -> str:
    """New brief: on resume — a fresh executor session (different fingerprint).

    review/rounds — before the review starts: the panel decides what the task is checked against, so it
    cannot change once a reviewer has run (`review_started()`). model — like `ahub model`, any inactive
    task: the executor is picked for the next session, review or not; a review task has no executor turn
    to pick, so its panel becomes the one reviewer.
    """
    t = _get(store, task_id)
    if t.state in (State.ACCEPTED, State.REJECTED) or t.state in transitions.ACTIVE:
        raise DecisionError(_t("accept.edit_state", label=t.label), hint=_t("hint.status_task", label=t.label))
    panel = list(t.review.get("models") or []) if t.kind is Kind.REVIEW else []
    if t.kind is Kind.REVIEW and review is not None and model:
        raise DecisionError(_t("tasks.review_both"))
    changes: list[str] = []
    fields: dict[str, Any] = {}
    limits = dict(t.limits)
    if review is not None or rounds is not None:
        if t.kind is Kind.REVIEW and rounds is not None:
            raise DecisionError(_t("tasks.review_no_rounds"))
        _panel_locked(store, t, "--rounds" if review is None else "--review")
        models = list(review if review is not None else (t.review.get("models") or []))
        count = int(rounds if rounds is not None else (t.review.get("rounds") or 0))
        if not models:
            raise DecisionError(_t("accept.need_review"), hint=_t("hint.models"))
        if rounds is None and not count:
            count = 1  # nothing asked for yet — one round is the default; an explicit 0 is refused below
        if not 1 <= count <= tasks.MAX_ROUNDS:
            raise DecisionError(_t("accept.edit_rounds_bad", rounds=count, max=tasks.MAX_ROUNDS),
                                hint=_t("hint.models"))
        bases: list[str] = []
        befforts: list[str] = []
        for alias in models:
            try:
                checked = registry.check(store, alias, project)
                bases.append(checked.alias)
                befforts.append(registry.stored_effort(alias))
            except registry.RegistryError as e:
                raise DecisionError(str(e), hint=_t("hint.models")) from e
        if t.kind is Kind.REVIEW:
            count = 1  # the same one round tasks.resolve gives a review task
        fields["review"] = {"models": bases, "rounds": count}
        if any(befforts):
            fields["review"]["efforts"] = befforts
        refs = [registry.model_ref(b, e) for b, e in zip(bases, befforts, strict=True)]
        changes.append(_t("accept.review_msg", models="+".join(refs), rounds=count))
    if model:
        try:
            checked = registry.check(store, model, project)
        except registry.RegistryError as e:
            raise DecisionError(str(e), hint=_t("hint.models")) from e
        ref = registry.model_ref(checked.alias, registry.stored_effort(model))
        cur_ref = registry.model_ref(registry.base_alias(t.executor or ""),
                                     getattr(t, "effort", "") or registry.stored_effort(t.executor or ""))
        if panel:
            # a review task with a panel runs that panel — a model here names the one reviewer of it
            _panel_locked(store, t, "--model")
            count = int(t.review.get("rounds") or 1)
            fields["review"] = {"models": [checked.alias], "rounds": count}
            if registry.stored_effort(model):
                fields["review"]["efforts"] = [registry.stored_effort(model)]
            changes.append(_t("accept.review_msg", models=ref, rounds=count))
        elif ref != cur_ref:
            changes.append(_t("accept.model_edit", old=cur_ref or "—", new=ref))
            fields["executor"] = checked.alias
            fields["effort"] = registry.stored_effort(model)
            limits["fresh_session"] = True  # never resume another model's session
    cur_input = input.strip() if input is not None else str(t.limits.get("input") or "")
    if input is not None:
        if t.kind is not Kind.REVIEW:
            raise DecisionError(_t("accept.input_not_review", label=t.label))
        if not input.strip():
            raise DecisionError(_t("tasks.need_input"))
        if cur_input != str(t.limits.get("input") or ""):
            limits["input"] = cur_input
            changes.append(_t("accept.input_edit", input=cur_input))
    new = tasks.TaskSpec(project=t.project, kind=t.kind, title=title if title is not None else t.title,
                         spec=spec if spec is not None else t.spec, result_format=t.result_format,
                         paths=list(t.limits.get("paths") or []), accept=list(t.limits.get("accept") or []),
                         review_input=cur_input)
    h = tasks.spec_hash(new)
    if h != t.spec_hash:
        limits["fresh_session"] = True
        fields |= {"title": new.title, "spec": new.spec, "spec_hash": h}
        changes.append(_t("accept.edit_msg"))
    if not changes:
        return _t("accept.edit_nothing", label=t.label)
    fields["limits"] = _clear_loop(_expire_quota_hold(limits))
    store.update_task(t.id, now=now_ms(), **fields)  # the parts of the one line the CLI prints
    store.add_event(Ev.STATE, task_id=t.id, project=t.project,
                    payload={"edit": ", ".join(changes), "by": by})
    return f"{t.label}: {', '.join(changes)}"


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
                  add_usd: float | None = None, set_usd: float | None = None, by: str = "orchestrator") -> str:
    """Set the budget in one move: raised (or lowered) + resumed (if the task was parked on budget).

    add/set_to — Go counter (subscription); add_usd/set_usd — real money (default 0 = no spending).
    set_usd cannot go below what the task already spent.
    """
    t = _get(store, task_id)
    new = set_to if set_to is not None else t.budget_go + (add or 0.0)
    new_usd = set_usd if set_usd is not None else t.budget_usd + (add_usd or 0.0)
    if set_usd is not None:
        spent = archive.task_cost(store, t.id)[1]
        if new_usd < spent:
            raise DecisionError(_t("accept.budget_below_spent", spent=f"{spent:.3f}"))
    # an explicit set is a change even when it lowers the budget
    if set_to is None and set_usd is None and new <= t.budget_go and new_usd <= t.budget_usd:
        raise DecisionError(_t("accept.budget_need"))
    store.update_task(t.id, budget_go=float(new), budget_usd=float(new_usd))
    store.add_event(Ev.BUDGET_EXTENDED, task_id=t.id, project=t.project,
                    payload={"from": t.budget_go, "to": new, "usd_from": t.budget_usd, "usd_to": new_usd, "by": by})
    msg = _t("accept.budget_msg", label=t.label, old=f"{t.budget_go:g}", new=f"{new:g}") + (
        _t("accept.budget_usd", old=f"{t.budget_usd:g}", new=f"{new_usd:g}")
        if new_usd != t.budget_usd else "")
    if t.state is State.NEEDS_DECISION and _stopped_by_budget(store, t.id):
        transitions.move(store, t.id, State.QUEUED, reason=reasons.dump("budget_extended"), by=by)
        events.ack_task(store, t.id)
        msg += _t("accept.budget_resumed")
    return msg


def change_model(store: Store, project: ProjectConfig, task_id: int, alias: str, *, by: str = "orchestrator") -> str:
    t = _get(store, task_id)
    if t.state in transitions.ACTIVE:
        raise DecisionError(_t("accept.model_active", label=t.label))
    try:
        checked = registry.check(store, alias, project)
    except registry.RegistryError as e:
        raise DecisionError(str(e), hint=_t("hint.models")) from e
    ref = registry.model_ref(checked.alias, registry.stored_effort(alias))
    cur_ref = registry.model_ref(registry.base_alias(t.executor or ""),
                                 getattr(t, "effort", "") or registry.stored_effort(t.executor or ""))
    panel = list(t.review.get("models") or []) if t.kind is Kind.REVIEW else []
    if panel:
        # what reviews is the panel (engine._review) — the executor is only the fallback reviewer
        _panel_locked(store, t, "ahub model")
        rounds = int(t.review.get("rounds") or 1)
        new_review: dict = {"models": [checked.alias], "rounds": rounds}
        if registry.stored_effort(alias):
            new_review["efforts"] = [registry.stored_effort(alias)]
        store.update_task(t.id, review=new_review, limits=_clear_loop(_expire_quota_hold(dict(t.limits))))
        store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                        payload={"from": ", ".join(panel), "to": ref, "by": by})
        return _t("accept.model_panel", label=t.label, old=", ".join(panel), new=ref)
    lim = dict(t.limits)
    lim["fresh_session"] = True  # never resume another model's session
    store.update_task(t.id, executor=checked.alias, effort=registry.stored_effort(alias),
                      limits=_clear_loop(_expire_quota_hold(lim)))
    store.add_event(Ev.MODEL_CHANGED, task_id=t.id, project=t.project,
                    payload={"from": cur_ref, "to": ref, "by": by})
    return _t("accept.model_msg", label=t.label, old=cur_ref or "—", new=ref)
