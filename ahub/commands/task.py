"""Task handles for the orchestrator: task new | status | result | log | stop | nudge | continue | accept | reject.

Compact output (contracts §5, §7); reading a task implicitly acks its events. A command that names one task belongs
to that task's project (ahub/scope.py): a task of another project is refused, with the way out (--project/--all).
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ahub import events, scope, tasks, transitions, views
from ahub.cliutil import CliError, add_project_arg, add_scope_args, check_task, emit, resolve_project
from ahub.model import ACTIVE, WAITING_DECISION, Kind, State, parse_task_id
from ahub.service import live_workers
from ahub.store import Store, Task


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _task(store: Store, ref: str, args) -> Task:
    """The task by its reference, checked against the scope of the handle (ahub/scope.py)."""
    from ahub.i18n import t as _t

    try:
        tid = parse_task_id(ref)
    except ValueError as e:
        raise CliError(str(e), hint=_t("hint.status")) from e
    t = store.get_task(tid)
    if t is None:
        raise CliError(_t("err.no_task", ref=ref), hint=_t("hint.status"))
    check_task(args, t)
    return t


def cmd_new(args) -> int:
    project = resolve_project(args)
    spec_text = args.spec or ""
    if args.spec_file:
        try:
            spec_text = Path(args.spec_file).read_text(encoding="utf-8")
        except OSError as e:
            from ahub.i18n import t as _t

            raise CliError(_t("err.spec_file", err=e), hint=_t("hint.status")) from e
    review_models = None
    if args.no_review:
        review_models = []
    elif args.review:
        review_models = _csv(args.review)
    spec = tasks.TaskSpec(
        project=project.name, kind=Kind(args.kind), title=args.title, spec=spec_text,
        result_format=args.format or "", model=args.model, review_level=args.level,
        review_models=review_models, review_rounds=args.rounds, paths=_csv(args.paths),
        accept=_csv(args.accept), read=_csv(args.read), review_input=args.input or "",
        resources=_csv(args.resources), after=[parse_task_id(a) for a in _csv(args.after)],
        budget_go=args.budget, budget_usd=args.budget_usd, time_limit_min=args.time_limit, created_by=args.by)
    store = Store()
    try:
        t = tasks.create(store, spec, project, key=args.key, draft=args.draft, collect=not args.no_collect)
    except tasks.TaskInvalid as e:
        from ahub.i18n import t as _t

        raise CliError(_t("err.task_invalid", errors="; ".join(e.errors)), hint=_t("hint.task_new")) from e
    from ahub.i18n import t as _t

    word = _t("task.word_draft") if t.state is State.DRAFT else _t("task.word_queued")
    if t.review:
        extra = _t("task.review_extra", models="+".join(t.review["models"]), rounds=t.review["rounds"])
    else:
        extra = ""
    text = _t("task.created", label=t.label, word=word, kind=t.kind.value, executor=t.executor, extra=extra)
    text += "\n" + views.next_line("views.next_new", t.label)
    emit(args, {"id": t.id, "label": t.label, "state": t.state.value}, text)
    return 0


def cmd_status(args) -> int:
    store = Store()
    live = live_workers()
    if args.task:
        t = _task(store, args.task, args)
        events.ack_task(store, t.id)
        from ahub import reasons

        emit(args, {"task": asdict(t), "live": t.id in live,
                    "state_reason_text": reasons.text(t.state_reason)},
             views.task_text(store, t, live=live))
        return 0
    sc = scope.resolve(args)
    from ahub import config, pulse, reasons

    projects, _errs = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects)
    text = views.status_text(store, scope=sc, live=live, pulses=pulses)
    queued = store.list_tasks(states={State.QUEUED}, projects=sc.projects)
    data = {"active": [asdict(t) for t in store.list_tasks(states=ACTIVE, projects=sc.projects)],
            "waiting": [asdict(t) for t in store.list_tasks(states=WAITING_DECISION, projects=sc.projects)],
            "queued": [{"id": t.id, "label": t.label, "state": t.state.value,
                        "reason": t.state_reason,
                        "reason_text": reasons.text(t.state_reason)} for t in queued],
            "project": sc.name, "live": live}
    emit(args, data, text)
    return 0


def cmd_result(args) -> int:
    store = Store()
    t = _task(store, args.task, args)
    events.ack_task(store, t.id)
    emit(args, {"task": asdict(t)}, views.result_text(store, t, full=args.full, max_bytes=args.max_bytes))
    return 0


def cmd_log(args) -> int:
    store = Store()
    t = _task(store, args.task, args)
    emit(args, {"sessions": [asdict(s) for s in store.list_sessions(t.id)]},
         views.log_text(store, t, max_bytes=args.max_bytes))
    return 0


def _project_of(store: Store, t: Task):
    from ahub.worker import find_project

    p = find_project(t.project)
    if p is None:
        from ahub.i18n import t as _t

        raise CliError(_t("err.project_missing", project=t.project), hint=_t("hint.setup_path", path=t.project))
    return p


def _decide(args, fn, next_key: str = "") -> int:
    """One decision command: its result line, and the command that follows it (when there is one)."""
    from ahub.accept import DecisionError

    store = Store()
    t = _task(store, args.task, args)
    try:
        msg = fn(store, t)
    except DecisionError as e:
        raise CliError(str(e), hint=getattr(e, "hint", "")) from e
    text = msg + ("\n" + views.next_line(next_key, t.label) if next_key else "")
    emit(args, {"id": t.id, "result": msg}, text)
    return 0


def cmd_continue(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.continue_task(s, t.id, by=args.by), "views.next_continue")


def cmd_accept(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.accept(s, _project_of(s, t), t.id, by=args.by), "views.next_accept")


def cmd_reject(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.reject(s, _project_of(s, t), t.id, reason=args.reason or "",
                                                    by=args.by, keep=args.keep), "views.next_reject")


def cmd_rework(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.rework(s, t.id, args.notes, by=args.by), "views.next_rework")


def cmd_edit(args) -> int:
    """A new brief (title/spec), and — before the review starts — a new panel and executor."""
    from ahub import accept

    spec = Path(args.spec_file).read_text(encoding="utf-8") if args.spec_file else args.spec
    review = _csv(args.review) or None
    return _decide(args, lambda s, t: accept.edit(s, _project_of(s, t), t.id, spec=spec, title=args.title,
                                                  review=review, rounds=args.rounds, model=args.model,
                                                  by=args.by))


def cmd_extend(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.extend_paths(s, _project_of(s, t), t.id, _csv(args.paths), by=args.by),
                   "views.next_task")


def cmd_budget(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.extend_budget(s, t.id, add=args.add, set_to=args.set,
                                                           add_usd=args.add_usd, by=args.by), "views.next_task")


def cmd_model(args) -> int:
    from ahub import accept

    return _decide(args, lambda s, t: accept.change_model(s, _project_of(s, t), t.id, args.alias, by=args.by),
                   "views.next_task")


def cmd_stop(args) -> int:
    store = Store()
    t = _task(store, args.task, args)
    from ahub import reasons
    from ahub.i18n import t as _t

    try:
        how = transitions.request_stop(store, t.id, reason=args.reason or reasons.dump("stop_command"),
                                       by=args.by)
    except transitions.TransitionError as e:
        raise CliError(str(e), hint=_t("hint.status_task", label=t.label)) from e
    text = _t("task.stopped", label=t.label) if how == "stopped" else _t("task.stop_requested", label=t.label)
    emit(args, {"id": t.id, "result": how}, text + "\n" + views.next_line("views.next_task", t.label))
    return 0


def cmd_nudge(args) -> int:
    """`ahub nudge T12 "text"` — a message to a working agent in its own session."""
    store = Store()
    t = _task(store, args.task, args)
    from ahub.i18n import t as _t

    try:
        transitions.request_nudge(store, t.id, text=args.text, by=args.by)
    except transitions.TransitionError as e:
        raise CliError(str(e), hint=_t("hint.status_task", label=t.label)) from e
    emit(args, {"id": t.id, "result": "requested", "text": args.text},
         _t("task.nudge_requested", label=t.label))
    return 0


def cmd_diff(args) -> int:
    from ahub import gates
    from ahub.i18n import t as _t

    store = Store()
    t = _task(store, args.task, args)
    if not t.worktree or not Path(t.worktree).is_dir():
        raise CliError(_t("err.no_worktree", label=t.label))
    base = gates.effective_base(_project_of(store, t), t)
    text = gates.diff_text(t.worktree, base, limit=args.max_bytes)
    emit(args, {"base": base, "diff": text}, text or _t("task.diff_empty"))
    return 0


def cmd_history(args) -> int:
    store = Store()
    sc = scope.resolve(args)
    done = [t for t in store.list_tasks(projects=sc.projects, newest_first=True)
            if t.state in views.HISTORY_STATES][: args.n]
    emit(args, {"tasks": [asdict(t) for t in done], "project": sc.name},
         views.history_text(store, scope=sc, limit=args.n))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("task", help=t("help.task"))
    sub = p.add_subparsers(dest="task_cmd", required=True)
    n = sub.add_parser("new", help=t("help.task_new"))
    add_project_arg(n)
    n.add_argument("--kind", required=True, choices=[k.value for k in Kind])
    n.add_argument("--title", required=True, help=t("help.task_new_title"))
    g = n.add_mutually_exclusive_group()
    g.add_argument("--spec", help=t("help.task_new_spec"))
    g.add_argument("--spec-file", help=t("help.task_new_spec_file"))
    n.add_argument("--format", help=t("help.task_new_format"))
    n.add_argument("--model")
    n.add_argument("--level", type=int, help=t("help.task_new_level"))
    n.add_argument("--review", help=t("help.task_new_review"))
    n.add_argument("--rounds", type=int)
    n.add_argument("--no-review", action="store_true")
    n.add_argument("--paths", help=t("help.task_new_paths"))
    n.add_argument("--accept", help=t("help.task_new_accept"))
    n.add_argument("--read", help=t("help.task_new_read"))
    n.add_argument("--input", help=t("help.task_new_input"))
    n.add_argument("--resources")
    n.add_argument("--after", help=t("help.task_new_after"))
    n.add_argument("--budget", type=float, help=t("help.task_new_budget"))
    n.add_argument("--budget-usd", type=float, help=t("help.task_new_budget_usd"))
    n.add_argument("--time-limit", type=int, help=t("help.task_new_time_limit"))
    n.add_argument("--key", help=t("help.task_new_key"))
    n.add_argument("--draft", action="store_true")
    n.add_argument("--no-collect", action="store_true", help=t("help.task_new_no_collect"))
    n.add_argument("--by", default="orchestrator")
    n.set_defaults(func=cmd_new)

    s = subparsers.add_parser("status", help=t("help.status"))
    s.add_argument("task", nargs="?")
    add_scope_args(s)
    s.set_defaults(func=cmd_status)
    r = subparsers.add_parser("result", help=t("help.result"))
    r.add_argument("task")
    r.add_argument("--full", action="store_true")
    r.add_argument("--max-bytes", type=int, default=views.L3_DEFAULT)
    add_scope_args(r)
    r.set_defaults(func=cmd_result)
    lg = subparsers.add_parser("log", help=t("help.log"))
    lg.add_argument("task")
    lg.add_argument("--max-bytes", type=int, default=8000)
    add_scope_args(lg)
    lg.set_defaults(func=cmd_log)
    nd = subparsers.add_parser("nudge", help=t("help.nudge"))
    nd.add_argument("task")
    nd.add_argument("text", help=t("help.nudge"))
    nd.add_argument("--by", default="orchestrator")
    add_scope_args(nd)
    nd.set_defaults(func=cmd_nudge)
    for name, fn, key in (("stop", cmd_stop, "help.stop"), ("continue", cmd_continue, "help.continue"),
                          ("accept", cmd_accept, "help.accept"), ("reject", cmd_reject, "help.reject")):
        x = subparsers.add_parser(name, help=t(key))
        x.add_argument("task")
        x.add_argument("--reason")
        x.add_argument("--by", default="orchestrator")
        if name == "reject":
            x.add_argument("--keep", action="store_true", help=t("help.reject_keep"))
        add_scope_args(x)
        x.set_defaults(func=fn)
    rw = subparsers.add_parser("rework", help=t("help.rework"))
    rw.add_argument("task")
    rw.add_argument("--notes", required=True)
    rw.add_argument("--by", default="orchestrator")
    add_scope_args(rw)
    rw.set_defaults(func=cmd_rework)
    ed = sub.add_parser("edit", help=t("help.task_edit"))
    ed.add_argument("task")
    ed.add_argument("--title")
    g2 = ed.add_mutually_exclusive_group()
    g2.add_argument("--spec")
    g2.add_argument("--spec-file")
    ed.add_argument("--review", help=t("help.task_edit_review"))
    ed.add_argument("--rounds", type=int, help=t("help.task_edit_rounds"))
    ed.add_argument("--model", help=t("help.task_edit_model"))
    ed.add_argument("--by", default="orchestrator")
    add_scope_args(ed)
    ed.set_defaults(func=cmd_edit)
    ex = subparsers.add_parser("extend", help=t("help.extend"))
    ex.add_argument("task")
    ex.add_argument("--paths", required=True)
    ex.add_argument("--by", default="orchestrator")
    add_scope_args(ex)
    ex.set_defaults(func=cmd_extend)
    bu = subparsers.add_parser("budget", help=t("help.budget"))
    bu.add_argument("task")
    gb = bu.add_mutually_exclusive_group()
    gb.add_argument("--add", type=float, help=t("help.budget_add"))
    gb.add_argument("--set", type=float, help=t("help.budget_set"))
    bu.add_argument("--add-usd", type=float, help=t("help.budget_add_usd"))
    bu.add_argument("--by", default="orchestrator")
    add_scope_args(bu)
    bu.set_defaults(func=cmd_budget)
    mo = subparsers.add_parser("model", help=t("help.model"))
    mo.add_argument("task")
    mo.add_argument("alias")
    mo.add_argument("--by", default="orchestrator")
    add_scope_args(mo)
    mo.set_defaults(func=cmd_model)
    df = subparsers.add_parser("diff", help=t("help.diff"))
    df.add_argument("task")
    df.add_argument("--max-bytes", type=int, default=views.L3_DEFAULT)
    add_scope_args(df)
    df.set_defaults(func=cmd_diff)
    hi = subparsers.add_parser("history", help=t("help.history"))
    hi.add_argument("-n", type=int, default=20)
    add_scope_args(hi)
    hi.set_defaults(func=cmd_history)
