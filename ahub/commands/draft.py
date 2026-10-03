"""ahub draft "text" | draft start N | draft cancel N | draft list — task in words, model fills in fields."""

from __future__ import annotations

from ahub import drafts, ui
from ahub.cliutil import CliError, add_project_arg, emit, resolve_project
from ahub.store import Store


def cmd_new(args) -> int:
    from ahub.i18n import t

    project = resolve_project(args)
    store = Store()
    # the model reads the project and fills the fields — a minute or three, one live line on a terminal
    with ui.Live(t("draft.drafting")) as p:
        did = drafts.create(store, project, args.text, source="cli")
        p.step()
    preview, nxt = drafts.preview(store, did), drafts.hint(store, did)
    emit(args, {"id": did}, "\n\n".join([preview, ui.styled(nxt, "dim")]) if nxt else preview)
    return 0


def cmd_start(args) -> int:
    from ahub import views
    from ahub.i18n import t

    project = resolve_project(args)
    try:
        tid = drafts.start(Store(), project, args.id)
    except ValueError as e:
        raise CliError(str(e), hint=t("draft.next_start", id=args.id)) from e
    emit(args, {"task": tid}, t("draft.queued", tid=tid) + "\n"
         + views.next_line("views.next_task", f"T{tid}"))
    return 0


def cmd_cancel(args) -> int:
    from ahub.i18n import t

    ok = drafts.cancel(Store(), args.id)
    emit(args, {"ok": ok}, t("draft.cancelled") if ok else t("draft.cancel_no"))
    return 0 if ok else 2


def cmd_list(args) -> int:
    from ahub.i18n import t

    rows = drafts.list_drafts(Store())
    if not rows:
        emit(args, {"drafts": rows}, t("draft.empty"))
        return 0
    body = ui.table([t("draft.col_id"), t("draft.col_status"), t("draft.col_words"), t("draft.col_task")],
                    [[f"#{r['id']}", r["status"], r["text"], f"T{r['task_id']}" if r["task_id"] else "—"]
                     for r in rows],
                    max_width=[4, 10, None, 6], indent=2)
    emit(args, {"drafts": rows}, body)
    return 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("draft", help=t("help.draft"))
    sub = p.add_subparsers(dest="draft_cmd", required=True)
    n = sub.add_parser("new")
    n.add_argument("text")
    add_project_arg(n)
    n.set_defaults(func=cmd_new)
    s = sub.add_parser("start")
    s.add_argument("id", type=int)
    add_project_arg(s)
    s.set_defaults(func=cmd_start)
    c = sub.add_parser("cancel")
    c.add_argument("id", type=int)
    c.set_defaults(func=cmd_cancel)
    ls = sub.add_parser("list")
    ls.set_defaults(func=cmd_list)
