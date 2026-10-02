"""Waiting and messaging: wait | watch | ack | inbox | say | ask | questions | alarms (contracts §6, §7)."""

from __future__ import annotations

import time

from ahub import comms, events
from ahub.cliutil import CliError, emit
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
    got = events.wait(store, timeout_s=parse_duration(args.timeout), project=args.project, who=args.who)
    if not got:
        emit(args, {"events": []}, "")
        return 3
    emit(args, {"events": got}, "\n".join(got))
    return 0


def cmd_watch(args) -> int:
    """Endless line stream for Monitor: each line is work for the orchestrator."""
    from ahub.i18n import t

    store = Store()
    pending = [e for e in events.unacked(store, args.project) if e.delivered_at is not None]
    if pending:
        tail = "; ".join(events.lines(store, pending[:3]))[:180]
        print(t("comms.unread", n=len(pending), text=tail), flush=True)
    last_touch = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_touch >= events.PRESENCE_TOUCH_S:
                events.touch(store, args.who, project=args.project or "", via="watch")
                last_touch = now
            batch = events.ready_batch(store, project=args.project)
            if batch:
                events.mark_delivered(store, [e.id for e in batch])
                for ln in events.lines(store, batch):
                    print(ln, flush=True)
            time.sleep(args.poll)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


def cmd_ack(args) -> int:
    store = Store()
    if args.ids == ["all"]:
        n = events.ack(store, project=args.project)
    else:
        try:
            ids = [int(x) for x in args.ids]
        except ValueError as e:
            from ahub.i18n import t

            raise CliError(t("err.ack_usage")) from e
        n = events.ack(store, ids)
    from ahub.i18n import t

    emit(args, {"acked": n}, t("comms.acked", n=n))
    return 0


def cmd_inbox(args) -> int:
    from ahub.i18n import t

    rows = comms.inbox(Store(), mark=not args.peek)
    text = "\n".join(f"#{r['id']} {r['text']}" for r in rows) or t("comms.inbox_empty")
    emit(args, {"messages": rows}, text)
    return 0


def cmd_say(args) -> int:
    from ahub.i18n import t

    mid = comms.say(Store(), args.text, project=args.project or "")
    emit(args, {"id": mid}, t("comms.sent") if mid else "")
    return 0


def cmd_ask(args) -> int:
    opts = [o.strip() for o in (args.options or "").split(",") if o.strip()]
    tid = None
    if args.task:
        from ahub.model import parse_task_id
        tid = parse_task_id(args.task)
    qid = comms.ask(Store(), args.text, opts, task_id=tid)
    from ahub.i18n import t

    emit(args, {"id": qid}, t("comms.asked", qid=qid))
    return 0


def cmd_questions(args) -> int:
    from ahub.i18n import t

    rows = comms.open_questions(Store())
    text = "\n".join(f"#{r['id']} {r['text']}" + (f" [{', '.join(r['options'])}]" if r['options'] else "")
                     for r in rows) or t("comms.questions_empty")
    emit(args, {"questions": rows}, text)
    return 0


def cmd_alarms(args) -> int:
    from ahub.i18n import t

    store = Store()
    al = comms.alarms(store, unacked_only=not args.all)
    lines = events.lines(store, al) or [t("comms.alarms_empty")]
    if args.ack and al:
        events.ack(store, [e.id for e in al])
    emit(args, {"alarms": [e.payload | {"id": e.id, "critical": e.critical} for e in al]}, "\n".join(lines))
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    w = subparsers.add_parser("wait", help=t("help.wait"))
    w.add_argument("--timeout", default="30m")
    w.add_argument("--project")
    w.add_argument("--who", default=events.DEFAULT_WHO)
    w.set_defaults(func=cmd_wait)
    wt = subparsers.add_parser("watch", help=t("help.watch"))
    wt.add_argument("--project")
    wt.add_argument("--who", default=events.DEFAULT_WHO)
    wt.add_argument("--poll", type=float, default=3.0)
    wt.set_defaults(func=cmd_watch)
    a = subparsers.add_parser("ack", help=t("help.ack"))
    a.add_argument("ids", nargs="+")
    a.add_argument("--project")
    a.set_defaults(func=cmd_ack)
    i = subparsers.add_parser("inbox", help=t("help.inbox"))
    i.add_argument("--peek", action="store_true", help=t("help.inbox_peek"))
    i.set_defaults(func=cmd_inbox)
    s = subparsers.add_parser("say", help=t("help.say"))
    s.add_argument("text")
    s.add_argument("--project")
    s.set_defaults(func=cmd_say)
    q = subparsers.add_parser("ask", help=t("help.ask"))
    q.add_argument("text")
    q.add_argument("--options")
    q.add_argument("--task")
    q.set_defaults(func=cmd_ask)
    qs = subparsers.add_parser("questions", help=t("help.questions"))
    qs.set_defaults(func=cmd_questions)
    al = subparsers.add_parser("alarms", help=t("help.alarms"))
    al.add_argument("--ack", action="store_true")
    al.add_argument("--all", action="store_true")
    al.set_defaults(func=cmd_alarms)
