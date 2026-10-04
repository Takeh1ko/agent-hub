"""Waiting and messaging: wait | watch | ack | inbox | say | ask | questions | alarms (contracts §6, §7).

Every command here reads what an orchestrator reads, so every one of them is scoped by project
(ahub/scope.py): the project of the current directory, --project X — X, --all — every project (the owner).
`say`/`ask` write into the scope of the directory they were run from.

`inbox <id>` and `questions <id>` read one row in full — the list views cut the text to a cell; the whole
one is here (nothing is marked read by that: only `inbox` reads the inbox).

`wait`/`watch` survive a broken poll (a Monitor must not die with a bad turn), but not endlessly: after
MAX_POLL_FAILURES failures in a row the stream gives up with one line and exit code 4.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from dataclasses import dataclass

from ahub import comms, events, log, scope, ui, views
from ahub.cliutil import CliError, add_project_arg, add_scope_args, emit
from ahub.store import Store
from ahub.time import now_ms
from ahub.time import parse_duration as _parse_duration

MAX_POLL_FAILURES = 20  # consecutive failures of a poll before the stream gives up
RETRY_S = 1.0  # sleep between retries of a failed poll
FAILED_RC = 4  # the poll never worked — not a timeout (3), not a refusal (2)
clock = time.monotonic  # the clock of the wait's deadline (a test moves it)


def parse_duration(text: str) -> float:
    """Wrap: ValueError from ahub.time becomes CliError (single-line error)."""
    try:
        return _parse_duration(text)
    except ValueError as e:
        raise CliError(str(e)) from e


@dataclass
class _Failures:
    """Consecutive failures of a poll: one log line per distinct error, a cap instead of an endless loop.

    A streak of the same error is one record in the log, not one per poll; a poll that works again resets the
    streak. `note` returns '' while the stream may keep going, else the one line to exit with.
    """
    tag: str
    limit: int = MAX_POLL_FAILURES
    count: int = 0
    last: str = ""

    def __post_init__(self) -> None:
        self._log = log.get(self.tag)

    def note(self, error: Exception) -> str:
        self.count += 1
        text = f"{type(error).__name__}: {error}" if str(error) else type(error).__name__
        if text != self.last:
            self.last = text
            self._log.warning("%s: %s", self.tag, text)  # once per distinct error, not per poll
        if self.count < self.limit:
            return ""
        from ahub.i18n import t

        return t("comms.poll_failed", n=self.count, cmd=self.tag, err=ui.clip(text, 120))

    def reset(self) -> None:
        self.count, self.last = 0, ""


def cmd_wait(args) -> int:
    """Block until an event or the deadline.

    A broken poll is retried (KeyboardInterrupt is not) — until the cap says it is hopeless. The timeout is
    the deadline: the first poll always happens (`--timeout 0` — take what is there right now), and past the
    deadline not one more poll is started.
    """
    store = Store()
    sc = scope.resolve(args)
    fails = _Failures("wait")
    deadline = clock() + parse_duration(args.timeout)
    got: list[str] = []
    first = True  # a zero timeout is one poll of what is already pending, not a refusal
    while True:
        left = max(0.0, deadline - clock())
        if left <= 0 and not first:
            break  # the timeout is what it was asked for — no poll past it
        first = False
        try:
            got = events.wait(store, timeout_s=left, scope=sc, who=args.who)
            break
        except (sqlite3.Error, OSError) as e:  # a broken poll must not kill the wait; the cap decides
            line = fails.note(e)
            if line:
                print(line, file=sys.stderr, flush=True)
                return FAILED_RC
            time.sleep(min(RETRY_S, max(0.0, deadline - clock())))  # a retry must not outlive the deadline
    if not got:
        emit(args, {"events": []}, "")
        return 3
    emit(args, {"events": got}, "\n".join(got))
    return 0


def cmd_watch(args) -> int:
    """Endless line stream for Monitor: each line is work for the orchestrator."""
    from ahub.i18n import t

    store = Store()
    sc = scope.resolve(args)
    pending = events.watch_start_summary(store, who=args.who, scope=sc)
    if pending:
        tail = "; ".join(events.lines(store, pending[:3]))[:180]
        print(t("comms.unread", n=len(pending), text=tail), flush=True)
    last_touch = 0.0
    fails = _Failures("watch")
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
            fails.reset()
            time.sleep(args.poll)
        except (KeyboardInterrupt, BrokenPipeError):
            return 0
        except (sqlite3.Error, OSError) as e:  # the Monitor must survive a broken turn, not die with it
            line = fails.note(e)
            if line:
                print(line, file=sys.stderr, flush=True)
                return FAILED_RC
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
            raise CliError(t("err.ack_usage"), hint=t("hint.ack")) from e
        n = events.ack(store, ids, scope=sc)
    emit(args, {"acked": n}, t("comms.acked", n=n))
    return 0


def _row_num(ref: str) -> int:
    """The number of a row: «36» or «#36»."""
    from ahub.i18n import t

    try:
        return int(ref.lstrip("#"))
    except ValueError as e:
        raise CliError(t("err.bad_ref", ref=ref)) from e


def _row(row: dict | None, sc: scope.Scope, kind: str, ref: str) -> dict:
    """One row of the scope by its number, or a refusal: no such row — or one of another project,
    with the way out (architecture §9 — the same rule as for a task)."""
    from ahub.i18n import t

    if row is None:
        raise CliError(t(f"err.no_{kind}", ref=f"#{ref.lstrip('#')}"))
    project = str(row.get("project") or "")
    if scope.foreign(sc, project):
        raise CliError(t("err.foreign_row", ref=f"#{row['id']}", project=project),
                       t("err.foreign_task_hint", project=project))
    return row


def cmd_inbox(args) -> int:
    store = Store()
    sc = scope.resolve(args)
    if args.id:
        row = _row(comms.message(store, _row_num(args.id)), sc, "message", args.id)
        emit(args, {"message": row}, views.message_text(row))
        return 0
    rows = comms.inbox(store, mark=not args.peek, scope=sc)
    emit(args, {"messages": rows}, views.inbox_text(rows, full=args.full))
    return 0


def cmd_say(args) -> int:
    from ahub.i18n import t

    if getattr(args, "all", False):
        raise CliError(t("err.scope_all_say"), hint=t("hint.say_project"))
    sc = scope.resolve(args)
    mid = comms.say(Store(), args.text, project=sc.name)
    emit(args, {"id": mid, "project": sc.name}, t("comms.sent") if mid else "")
    return 0


def cmd_ask(args) -> int:
    from ahub.i18n import t

    if getattr(args, "all", False):
        raise CliError(t("err.scope_all_ask"), hint=t("hint.ask_project"))
    opts = [o.strip() for o in (args.options or "").split(",") if o.strip()]
    tid = None
    if args.task:
        from ahub.model import parse_task_id

        tid = parse_task_id(args.task)
    qid = comms.ask(Store(), args.text, opts, task_id=tid, project=scope.resolve(args).name)
    emit(args, {"id": qid}, t("comms.asked", qid=qid))
    return 0


def cmd_questions(args) -> int:
    store = Store()
    sc = scope.resolve(args)
    if args.id:
        row = _row(comms.question(store, _row_num(args.id)), sc, "question", args.id)
        emit(args, {"question": row}, views.question_text(row))
        return 0
    rows = comms.open_questions(store, scope=sc)
    emit(args, {"questions": rows}, views.questions_text(rows))
    return 0


def cmd_alarms(args) -> int:
    """Every alarm with its event id and age — so it can be acked (`ahub ack <id>`) or all at once."""
    from ahub import ui
    from ahub.i18n import plural, t

    store = Store()
    sc = scope.resolve(args)
    al = comms.alarms(store, unacked_only=not args.acked, scope=sc)
    if not al:
        emit(args, {"alarms": []}, t("comms.alarms_empty"))
        return 0
    now = now_ms()
    head = [t("alarms.col_id"), t("alarms.col_age"), t("alarms.col_what")]
    body = [[f"#{e.id}", views.age(e.ts, now), line] for e, line in zip(al, events.lines(store, al), strict=True)]
    out = [ui.table(head, body, max_width=[6, 8, None], indent=2)]
    if args.ack:  # the result of the command, not a suggestion for the next one
        events.ack(store, [e.id for e in al], scope=sc)
        out.append(ui.styled(plural(len(al), "alarms.acked_one", "alarms.acked_few", "alarms.acked_many"), "dim"))
    else:
        out.append(ui.styled(ui.kv([(t("views.lbl_next"), t("alarms.next"))]), "dim"))
    emit(args, {"alarms": [e.payload | {"id": e.id, "critical": e.critical} for e in al]}, "\n".join(out))
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
    i.add_argument("id", nargs="?", help=t("help.row_id"))
    i.add_argument("--peek", action="store_true", help=t("help.inbox_peek"))
    i.add_argument("--full", action="store_true", help=t("help.inbox_full"))
    add_scope_args(i)
    i.set_defaults(func=cmd_inbox)
    s = subparsers.add_parser("say", help=t("help.say"))
    s.add_argument("text")
    add_project_arg(s)
    s.set_defaults(func=cmd_say)
    q = subparsers.add_parser("ask", help=t("help.ask"))
    q.add_argument("text")
    q.add_argument("--options")
    q.add_argument("--task")
    add_project_arg(q)
    q.set_defaults(func=cmd_ask)
    qs = subparsers.add_parser("questions", help=t("help.questions"))
    qs.add_argument("id", nargs="?", help=t("help.row_id"))
    add_scope_args(qs)
    qs.set_defaults(func=cmd_questions)
    al = subparsers.add_parser("alarms", help=t("help.alarms"))
    al.add_argument("--ack", action="store_true")
    al.add_argument("--acked", action="store_true", help=t("help.alarms_acked"))
    add_scope_args(al)
    al.set_defaults(func=cmd_alarms)
