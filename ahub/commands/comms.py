"""Waiting and messaging: wait | watch | ack | inbox | say | ask | questions | alarms (contracts §6, §7).

Every command here reads what an orchestrator reads, so every one of them is scoped by project
(ahub/scope.py): the project of the current directory, --project X — X, --all — every project (the owner).
`say`/`ask` write into the scope of the directory they were run from.
"""

from __future__ import annotations

import time

from ahub import comms, events, log, scope, views
from ahub.cliutil import CliError, add_scope_args, emit
from ahub.store import Store
from ahub.time import parse_duration as _parse_duration


def parse_duration(text: str) -> float:
    """Wrap: ValueError from ahub.time becomes CliError (single-line error)."""
    try:
        return _parse_duration(text)
    except ValueError as e:
        raise CliError(str(e)) from e


def cmd_wait(args) -> int:
    store = Store()
    sc = scope.resolve(args)
    got = events.wait(store, timeout_s=parse_duration(args.timeout), scope=sc, who=args.who)
    if not got:
        emit(args, {"events": []}, "")
        return 3
    emit(args, {"events": got}, "\n".join(got))
    return 0


def cmd_watch(args) -> int:
    """Endless line stream for Monitor: each line is work for the orchestrator."""
    from ahub.i18n import t

    _log = log.get("watch")
    store = Store()
    sc = scope.resolve(args)
    pending = events.watch_start_summary(store, who=args.who, scope=sc)
    if pending:
        tail = "; ".join(events.lines(store, pending[:3]))[:180]
        print(t("comms.unread", n=len(pending), text=tail), flush=True)
    last_touch = 0.0
    names = events.presence_projects(sc)  # the owner's projects are read from the config once, not per touch
    while True:
        try:
            now = time.monotonic()
            if now - last_touch >= events.PRESENCE_TOUCH_S:
                # a failed presence stamp is a log line, never the end of the stream
                events.touch_scope(store, sc, args.who, via="watch", names=names)
                last_touch = now
            batch = events.ready_batch(store, scope=sc)
            if batch:
                events.mark_delivered(store, [e.id for e in batch])
                for ln in events.lines(store, batch):
                    print(ln, flush=True)
            time.sleep(args.poll)
        except (KeyboardInterrupt, BrokenPipeError):
            return 0
        except Exception as e:  # the Monitor must survive a broken turn, not die with it
            _log.warning("watch: %s", e)
            time.sleep(args.poll)


def cmd_ack(args) -> int:
    from ahub.i18n import t

    store = Store()
    sc = scope.resolve(args)
    if args.ids == ["all"]:
        n = events.ack(store, scope=sc)
    else:
        try:
            ids = [int(x) for x in args.ids]
        except ValueError as e:
            raise CliError(t("err.ack_usage")) from e
        n = events.ack(store, ids, scope=sc)
    emit(args, {"acked": n}, t("comms.acked", n=n))
    return 0


def cmd_inbox(args) -> int:
    rows = comms.inbox(Store(), mark=not args.peek, scope=scope.resolve(args))
    emit(args, {"messages": rows}, views.inbox_text(rows))
    return 0


def cmd_say(args) -> int:
    from ahub.i18n import t

    sc = scope.resolve(args)
    mid = comms.say(Store(), args.text, project=sc.name)
    emit(args, {"id": mid, "project": sc.name}, t("comms.sent") if mid else "")
    return 0


def cmd_ask(args) -> int:
    from ahub.i18n import t

    opts = [o.strip() for o in (args.options or "").split(",") if o.strip()]
    tid = None
    if args.task:
        from ahub.model import parse_task_id

        tid = parse_task_id(args.task)
    qid = comms.ask(Store(), args.text, opts, task_id=tid, project=scope.resolve(args).name)
    emit(args, {"id": qid}, t("comms.asked", qid=qid))
    return 0


def cmd_questions(args) -> int:
    rows = comms.open_questions(Store(), scope=scope.resolve(args))
    emit(args, {"questions": rows}, views.questions_text(rows))
    return 0


def cmd_alarms(args) -> int:
    from ahub.i18n import t

    store = Store()
    sc = scope.resolve(args)
    al = comms.alarms(store, unacked_only=not args.acked, scope=sc)
    lines = events.lines(store, al) or [t("comms.alarms_empty")]
    if args.ack and al:
        events.ack(store, [e.id for e in al], scope=sc)
    emit(args, {"alarms": [e.payload | {"id": e.id, "critical": e.critical} for e in al]}, "\n".join(lines))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    w = subparsers.add_parser("wait", help=t("help.wait"))
    w.add_argument("--timeout", default="30m")
    add_scope_args(w)
    w.add_argument("--who", default=events.DEFAULT_WHO)
    w.set_defaults(func=cmd_wait)
    wt = subparsers.add_parser("watch", help=t("help.watch"))
    add_scope_args(wt)
    wt.add_argument("--who", default=events.DEFAULT_WHO)
    wt.add_argument("--poll", type=float, default=3.0)
    wt.set_defaults(func=cmd_watch)
    a = subparsers.add_parser("ack", help=t("help.ack"))
    a.add_argument("ids", nargs="+")
    add_scope_args(a)
    a.set_defaults(func=cmd_ack)
    i = subparsers.add_parser("inbox", help=t("help.inbox"))
    i.add_argument("--peek", action="store_true", help=t("help.inbox_peek"))
    add_scope_args(i)
    i.set_defaults(func=cmd_inbox)
    s = subparsers.add_parser("say", help=t("help.say"))
    s.add_argument("text")
    add_scope_args(s)
    s.set_defaults(func=cmd_say)
    q = subparsers.add_parser("ask", help=t("help.ask"))
    q.add_argument("text")
    q.add_argument("--options")
    q.add_argument("--task")
    add_scope_args(q)
    q.set_defaults(func=cmd_ask)
    qs = subparsers.add_parser("questions", help=t("help.questions"))
    add_scope_args(qs)
    qs.set_defaults(func=cmd_questions)
    al = subparsers.add_parser("alarms", help=t("help.alarms"))
    al.add_argument("--ack", action="store_true")
    al.add_argument("--acked", action="store_true", help=t("help.alarms_acked"))
    add_scope_args(al)
    al.set_defaults(func=cmd_alarms)
