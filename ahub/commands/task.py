"""Ручки задач для оркестратора: task new | status | result | log | stop | continue | accept | reject.

Вывод компактный (contracts §5, §7); чтение задачи неявно подтверждает её события.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ahub import archive, events, tasks, transitions, views, workspace
from ahub.cliutil import CliError, add_project_arg, emit, resolve_project
from ahub.model import ACTIVE, CHANGES_FILES, Kind, State, WAITING_DECISION, parse_task_id
from ahub.service import live_workers
from ahub.store import Store, Task


def _csv(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def _task(store: Store, ref: str) -> Task:
    try:
        tid = parse_task_id(ref)
    except ValueError as e:
        raise CliError(str(e)) from e
    t = store.get_task(tid)
    if t is None:
        from ahub.i18n import t as _t

        raise CliError(_t("err.no_task", ref=ref))
    return t


def cmd_new(args) -> int:
    project = resolve_project(args)
    spec_text = args.spec or ""
    if args.spec_file:
        try:
            spec_text = Path(args.spec_file).read_text(encoding="utf-8")
        except OSError as e:
            from ahub.i18n import t as _t

            raise CliError(_t("err.spec_file", err=e)) from e
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

        raise CliError(_t("err.task_invalid", errors="; ".join(e.errors))) from e
    from ahub.i18n import t as _t

    word = _t("task.word_draft") if t.state is State.DRAFT else _t("task.word_queued")
    if t.review:
        extra = _t("task.review_extra", models="+".join(t.review["models"]), rounds=t.review["rounds"])
    else:
        extra = ""
    emit(args, {"id": t.id, "label": t.label, "state": t.state.value},
         _t("task.created", label=t.label, word=word, kind=t.kind.value, executor=t.executor, extra=extra))
    return 0


def cmd_status(args) -> int:
    store = Store()
    live = live_workers()
    if args.task:
        t = _task(store, args.task)
        events.ack_task(store, t.id)
        emit(args, {"task": asdict(t), "live": t.id in live}, views.task_text(store, t, live=live))
        return 0
    project = None
    if args.project:
        project = resolve_project(args).name
    from ahub import config, pulse

    projects, _errs = config.load_projects()
    pulses = pulse.all_pulses(store, live=live, projects=projects)
    text = views.status_text(store, project=project, live=live, pulses=pulses)
    data = {"active": [asdict(t) for t in store.list_tasks(states=ACTIVE, project=project)],
            "waiting": [asdict(t) for t in store.list_tasks(states=WAITING_DECISION, project=project)],
            "live": live}
    emit(args, data, text)
    return 0


def cmd_result(args) -> int:
    store = Store()
    t = _task(store, args.task)
    events.ack_task(store, t.id)
    emit(args, {"task": asdict(t)}, views.result_text(store, t, full=args.full, max_bytes=args.max_bytes))
    return 0


def cmd_log(args) -> int:
    store = Store()
    t = _task(store, args.task)
    emit(args, {"sessions": [asdict(s) for s in store.list_sessions(t.id)]},
         views.log_text(store, t, max_bytes=args.max_bytes))
    return 0


def cmd_stop(args) -> int:
    store = Store()
    t = _task(store, args.task)
    from ahub.i18n import t as _t

    try:
        how = transitions.request_stop(store, t.id, reason=args.reason or "остановлена командой", by=args.by)
    except transitions.TransitionError as e:
        raise CliError(str(e)) from e
    if how == "stopped":
        text = _t("task.stopped", label=t.label)
    else:
        text = _t("task.stop_requested", label=t.label)
    emit(args, {"id": t.id, "result": how}, text)
    return 0


def _project_of(store: Store, t: Task):
    from ahub.worker import find_project

    p = find_project(t.project)
    if p is None:
        from ahub.i18n import t as _t

        raise CliError(_t("err.project_missing", project=t.project))
    return p


def _decide(args, fn) -> int:
    from ahub.accept import DecisionError

    store = Store()
    t = _task(store, args.task)
    try:
        msg = fn(store, t)
    except DecisionError as e:
        raise CliError(str(e)) from e
    emit(args, {"id": t.id, "result": msg}, msg)
    return 0


def cmd_continue(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.continue_task(s, t.id, by=args.by))


def cmd_accept(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.accept(s, _project_of(s, t), t.id, by=args.by))


def cmd_reject(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.reject(s, _project_of(s, t), t.id, reason=args.reason or "",
                                                    by=args.by, keep=args.keep))


def cmd_rework(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.rework(s, t.id, args.notes, by=args.by))


def cmd_edit(args) -> int:
    from ahub import accept
    spec = Path(args.spec_file).read_text(encoding="utf-8") if args.spec_file else args.spec
    return _decide(args, lambda s, t: accept.edit(s, _project_of(s, t), t.id, spec=spec, title=args.title,
                                                  by=args.by))


def cmd_extend(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.extend_paths(s, _project_of(s, t), t.id, _csv(args.paths), by=args.by))


def cmd_budget(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.extend_budget(s, t.id, add=args.add, set_to=args.set,
                                                           add_usd=args.add_usd, by=args.by))


def cmd_model(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.change_model(s, _project_of(s, t), t.id, args.alias, by=args.by))


def cmd_diff(args) -> int:
    from ahub import gates
    from ahub.i18n import t as _t

    store = Store()
    t = _task(store, args.task)
    if not t.worktree or not Path(t.worktree).is_dir():
        raise CliError(_t("err.no_worktree", label=t.label))
    base = gates.effective_base(_project_of(store, t), t)
    text = gates.diff_text(t.worktree, base, limit=args.max_bytes)
    emit(args, {"base": base, "diff": text}, text or _t("task.diff_empty"))
    return 0


def cmd_history(args) -> int:
    from ahub.i18n import t as _t

    store = Store()
    project = resolve_project(args).name if args.project else None
    done = [t for t in store.list_tasks(project=project, newest_first=True) if t.state.value in
            ("accepted", "rejected", "done", "needs_decision", "error", "stopped")][: args.n]
    lines = []
    for t in done:
        go, usd = archive.task_cost(store, t.id)
        dur = ""
        if t.finished_at:
            dur = _t("task.history_dur", age=views._age(t.created_at, t.finished_at))
        lines.append(_t("task.history_line", label=t.label, kind=t.kind.value,
                        title=views._short(t.title, 45),
                        state=archive.STATE_WORDS.get(t.state.value, t.state.value),
                        round=t.round, cost=f"{go + usd:.3f}", dur=dur))
    emit(args, {"tasks": [asdict(t) for t in done]}, "\n".join(lines) or _t("task.history_empty"))
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
    add_project_arg(s)
    s.set_defaults(func=cmd_status)
    r = subparsers.add_parser("result", help=t("help.result"))
    r.add_argument("task")
    r.add_argument("--full", action="store_true")
    r.add_argument("--max-bytes", type=int, default=views.L3_DEFAULT)
    r.set_defaults(func=cmd_result)
    lg = subparsers.add_parser("log", help=t("help.log"))
    lg.add_argument("task")
    lg.add_argument("--max-bytes", type=int, default=8000)
    lg.set_defaults(func=cmd_log)
    for name, fn, key in (("stop", cmd_stop, "help.stop"), ("continue", cmd_continue, "help.continue"),
                          ("accept", cmd_accept, "help.accept"), ("reject", cmd_reject, "help.reject")):
        x = subparsers.add_parser(name, help=t(key))
        x.add_argument("task")
        x.add_argument("--reason")
        x.add_argument("--by", default="orchestrator")
        if name == "reject":
            x.add_argument("--keep", action="store_true", help=t("help.reject_keep"))
        x.set_defaults(func=fn)
    rw = subparsers.add_parser("rework", help=t("help.rework"))
    rw.add_argument("task")
    rw.add_argument("--notes", required=True)
    rw.add_argument("--by", default="orchestrator")
    rw.set_defaults(func=cmd_rework)
    ed = sub.add_parser("edit", help=t("help.task_edit"))
    ed.add_argument("task")
    ed.add_argument("--title")
    g2 = ed.add_mutually_exclusive_group()
    g2.add_argument("--spec")
    g2.add_argument("--spec-file")
    ed.add_argument("--by", default="orchestrator")
    ed.set_defaults(func=cmd_edit)
    ex = subparsers.add_parser("extend", help=t("help.extend"))
    ex.add_argument("task")
    ex.add_argument("--paths", required=True)
    ex.add_argument("--by", default="orchestrator")
    ex.set_defaults(func=cmd_extend)
    bu = subparsers.add_parser("budget", help=t("help.budget"))
    bu.add_argument("task")
    gb = bu.add_mutually_exclusive_group()
    gb.add_argument("--add", type=float, help=t("help.budget_add"))
    gb.add_argument("--set", type=float, help=t("help.budget_set"))
    bu.add_argument("--add-usd", type=float, help=t("help.budget_add_usd"))
    bu.add_argument("--by", default="orchestrator")
    bu.set_defaults(func=cmd_budget)
    mo = subparsers.add_parser("model", help=t("help.model"))
    mo.add_argument("task")
    mo.add_argument("alias")
    mo.add_argument("--by", default="orchestrator")
    mo.set_defaults(func=cmd_model)
    df = subparsers.add_parser("diff", help=t("help.diff"))
    df.add_argument("task")
    df.add_argument("--max-bytes", type=int, default=views.L3_DEFAULT)
    df.set_defaults(func=cmd_diff)
    hi = subparsers.add_parser("history", help=t("help.history"))
    hi.add_argument("-n", type=int, default=20)
    add_project_arg(hi)
    hi.set_defaults(func=cmd_history)
