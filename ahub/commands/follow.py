"""`ahub follow T12` — a readable live transcript of a worker session (`ahub log T12` stays raw).

The transcript comes from the raw session log plus the prompts sidecar next to it (ahub/transcript.py).
By default the latest session of the task is printed and then followed until the task leaves an active
state (a finished task — nothing to follow, the last session is printed once).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from ahub import providers, transcript
from ahub.cliutil import CliError, add_scope_args
from ahub.i18n import t as _t
from ahub.model import ACTIVE, Role
from ahub.service import live_workers
from ahub.store import Session, Store, Task

POLL_S = 1.0


def _archive_hint(t: Task) -> str:
    """Where the archive of a task with no worktree lives (the archive keeps the result, not the log)."""
    from ahub import archive
    from ahub.worker import find_project

    project = find_project(t.project)
    return str(archive.root(project) / "tasks" / t.label) if project is not None else ""


def _pick(sessions: list[Session], role: str | None, round_no: int | None) -> Session | None:
    """Latest session of the task, narrowed by --role / --round."""
    rows = [s for s in sessions if (role is None or s.role == role) and (round_no is None or s.round == round_no)]
    return rows[-1] if rows else None


def _still_active(store: Store, task_id: int) -> bool:
    """Follow while the task is active and its owner is alive — a dead worker must not hang the tail."""
    t = store.get_task(task_id)
    if t is None or t.state not in ACTIVE:
        return False
    sessions = store.list_sessions(task_id)
    if not sessions or sessions[-1].ended_at is None:
        return True  # a turn is running right now
    return task_id in live_workers()  # between turns the task process decides what happens next


def cmd_follow(args) -> int:
    from ahub.commands.task import _task

    store = Store()
    t = _task(store, args.task, args)
    session = _pick(store.list_sessions(t.id), args.role, args.round)
    if session is None:
        raise CliError(_t("follow.no_sessions", label=t.label))
    log = Path(session.log_path) if session.log_path else None
    if log is None or not log.is_file():
        where = _archive_hint(t)
        raise CliError(_t("follow.archived", label=t.label, path=where) if where
                       else _t("follow.no_log", label=t.label))
    try:
        provider = providers.get(session.provider)
    except KeyError:
        raise CliError(_t("follow.unknown_provider", provider=session.provider)) from None

    out = sys.stdout
    writer = transcript.Writer(out, full=args.full, color=out.isatty())
    out.write(_t("follow.session", label=t.label, role=session.role, model=session.model,
                 round=session.round, sid=session.external_id or "—") + "\n")
    out.write(transcript.dim(_t("follow.log", path=session.log_path), out.isatty()) + "\n")
    reader = transcript.Reader(provider, log)
    follow = not args.no_follow
    try:
        while True:
            writer.write(reader.read())
            if not follow or not _still_active(store, t.id):
                break
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        writer.flush()
        print(flush=True)
        return 130
    except BrokenPipeError:  # `ahub follow T1 | head`
        return 0
    writer.flush()
    if follow:
        row = next((s for s in reversed(store.list_sessions(t.id)) if s.id == session.id), session)
        out.write(_t("follow.end", outcome=row.outcome or row.status) + "\n")
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("follow", help=t("help.follow"))
    p.add_argument("task")
    p.add_argument("--role", choices=[Role.EXECUTOR.value, Role.REVIEWER.value, Role.SCOUT.value,
                                      Role.ROUTINE.value], help=t("help.follow_role"))
    p.add_argument("--round", type=int, help=t("help.follow_round"))
    p.add_argument("--full", action="store_true", help=t("help.follow_full"))
    p.add_argument("--no-follow", action="store_true", help=t("help.follow_no_follow"))
    add_scope_args(p)
    p.set_defaults(func=cmd_follow)
