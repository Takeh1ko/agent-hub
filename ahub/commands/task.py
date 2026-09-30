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
        raise CliError(f"нет задачи {ref}")
    return t


def cmd_new(args) -> int:
    project = resolve_project(args)
    spec_text = args.spec or ""
    if args.spec_file:
        try:
            spec_text = Path(args.spec_file).read_text(encoding="utf-8")
        except OSError as e:
            raise CliError(f"--spec-file: {e}") from e
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
        budget_go=args.budget, time_limit_min=args.time_limit, created_by=args.by)
    store = Store()
    try:
        t = tasks.create(store, spec, project, key=args.key, draft=args.draft, collect=not args.no_collect)
    except tasks.TaskInvalid as e:
        raise CliError("задача не создана: " + "; ".join(e.errors)) from e
    word = "черновик" if t.state is State.DRAFT else "в очереди"
    extra = f", ревью {'+'.join(t.review['models'])}×{t.review['rounds']}" if t.review else ""
    emit(args, {"id": t.id, "label": t.label, "state": t.state.value},
         f"{t.label} {word} ({t.kind.value}, {t.executor}{extra})")
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
    try:
        how = transitions.request_stop(store, t.id, reason=args.reason or "остановлена командой", by=args.by)
    except transitions.TransitionError as e:
        raise CliError(str(e)) from e
    emit(args, {"id": t.id, "result": how},
         f"{t.label}: " + ("остановлена" if how == "stopped" else "попросили процесс остановиться"))
    return 0


def _project_of(store: Store, t: Task):
    from ahub.worker import find_project

    p = find_project(t.project)
    if p is None:
        raise CliError(f"проект {t.project} не найден в конфиге хаба")
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
    return _decide(args, lambda s, t: accept.extend_budget(s, t.id, add=args.add, set_to=args.set, by=args.by))


def cmd_model(args) -> int:
    from ahub import accept
    return _decide(args, lambda s, t: accept.change_model(s, _project_of(s, t), t.id, args.alias, by=args.by))


def cmd_diff(args) -> int:
    from ahub import gates
    store = Store()
    t = _task(store, args.task)
    if not t.worktree or not Path(t.worktree).is_dir():
        raise CliError(f"{t.label}: копии нет (дифф принятой задачи — в архиве проекта)")
    base = gates.effective_base(_project_of(store, t), t)
    text = gates.diff_text(t.worktree, base, limit=args.max_bytes)
    emit(args, {"base": base, "diff": text}, text or "дифф пуст")
    return 0


def cmd_history(args) -> int:
    store = Store()
    project = resolve_project(args).name if args.project else None
    done = [t for t in store.list_tasks(project=project, newest_first=True) if t.state.value in
            ("accepted", "rejected", "done", "needs_decision", "error", "stopped")][: args.n]
    lines = []
    for t in done:
        go, usd = archive.task_cost(store, t.id)
        dur = ""
        if t.finished_at:
            dur = f" · {(t.finished_at - t.created_at) // 60000} мин"
        lines.append(f"{t.label} {t.kind.value} «{views._short(t.title, 45)}» · "
                     f"{archive.STATE_WORDS.get(t.state.value, t.state.value)} · круг {t.round} · ${go + usd:.3f}{dur}")
    emit(args, {"tasks": [asdict(t) for t in done]}, "\n".join(lines) or "истории нет")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("task", help="задачи")
    sub = p.add_subparsers(dest="task_cmd", required=True)
    n = sub.add_parser("new", help="новая задача")
    add_project_arg(n)
    n.add_argument("--kind", required=True, choices=[k.value for k in Kind])
    n.add_argument("--title", required=True, help="цель, одна фраза")
    g = n.add_mutually_exclusive_group()
    g.add_argument("--spec", help="описание")
    g.add_argument("--spec-file", help="описание из файла")
    n.add_argument("--format", help="какой нужен результат")
    n.add_argument("--model")
    n.add_argument("--level", type=int, help="уровень ревью 0–4")
    n.add_argument("--review", help="модели ревью через запятую")
    n.add_argument("--rounds", type=int)
    n.add_argument("--no-review", action="store_true")
    n.add_argument("--paths", help="разрешённые файлы (glob через запятую)")
    n.add_argument("--accept", help="pytest-ноды приёмки через запятую")
    n.add_argument("--read", help="что прочитать первым")
    n.add_argument("--input", help="вход для ревью: ветка, sha, a..b или файлы")
    n.add_argument("--resources")
    n.add_argument("--after", help="T3,T4")
    n.add_argument("--budget", type=float, help="бюджет Go, $")
    n.add_argument("--time-limit", type=int, help="минут")
    n.add_argument("--key", help="ключ идемпотентности")
    n.add_argument("--draft", action="store_true")
    n.add_argument("--no-collect", action="store_true", help="не проверять сбор приёмки pytest'ом")
    n.add_argument("--by", default="orchestrator")
    n.set_defaults(func=cmd_new)

    s = subparsers.add_parser("status", help="сводка (L1) или задача (L2)")
    s.add_argument("task", nargs="?")
    add_project_arg(s)
    s.set_defaults(func=cmd_status)
    r = subparsers.add_parser("result", help="результат задачи (L2; --full — L3)")
    r.add_argument("task")
    r.add_argument("--full", action="store_true")
    r.add_argument("--max-bytes", type=int, default=views.L3_DEFAULT)
    r.set_defaults(func=cmd_result)
    lg = subparsers.add_parser("log", help="сырые логи сессий задачи (L3)")
    lg.add_argument("task")
    lg.add_argument("--max-bytes", type=int, default=8000)
    lg.set_defaults(func=cmd_log)
    for name, fn, helptext in (("stop", cmd_stop, "остановить"), ("continue", cmd_continue, "продолжить"),
                               ("accept", cmd_accept, "принять (код — слить)"), ("reject", cmd_reject, "отклонить")):
        x = subparsers.add_parser(name, help=helptext)
        x.add_argument("task")
        x.add_argument("--reason")
        x.add_argument("--by", default="orchestrator")
        if name == "reject":
            x.add_argument("--keep", action="store_true", help="не удалять копию задачи")
        x.set_defaults(func=fn)
    rw = subparsers.add_parser("rework", help="вернуть на доработку с указаниями")
    rw.add_argument("task")
    rw.add_argument("--notes", required=True)
    rw.add_argument("--by", default="orchestrator")
    rw.set_defaults(func=cmd_rework)
    ed = sub.add_parser("edit", help="новая постановка (продолжение — новой сессией)")
    ed.add_argument("task")
    ed.add_argument("--title")
    g2 = ed.add_mutually_exclusive_group()
    g2.add_argument("--spec")
    g2.add_argument("--spec-file")
    ed.add_argument("--by", default="orchestrator")
    ed.set_defaults(func=cmd_edit)
    ex = subparsers.add_parser("extend", help="расширить разрешённые файлы задачи")
    ex.add_argument("task")
    ex.add_argument("--paths", required=True)
    ex.add_argument("--by", default="orchestrator")
    ex.set_defaults(func=cmd_extend)
    bu = subparsers.add_parser("budget", help="продлить бюджет задачи (стоявшая из-за бюджета — продолжится)")
    bu.add_argument("task")
    gb = bu.add_mutually_exclusive_group(required=True)
    gb.add_argument("--add", type=float)
    gb.add_argument("--set", type=float)
    bu.add_argument("--by", default="orchestrator")
    bu.set_defaults(func=cmd_budget)
    mo = subparsers.add_parser("model", help="сменить модель задачи")
    mo.add_argument("task")
    mo.add_argument("alias")
    mo.add_argument("--by", default="orchestrator")
    mo.set_defaults(func=cmd_model)
    df = subparsers.add_parser("diff", help="дифф задачи от базы (L3)")
    df.add_argument("task")
    df.add_argument("--max-bytes", type=int, default=views.L3_DEFAULT)
    df.set_defaults(func=cmd_diff)
    hi = subparsers.add_parser("history", help="недавние задачи")
    hi.add_argument("-n", type=int, default=20)
    add_project_arg(hi)
    hi.set_defaults(func=cmd_history)
