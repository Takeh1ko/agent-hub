"""ahub draft "текст" | draft start N | draft cancel N | draft list — задача словами, поля дописывает модель."""

from __future__ import annotations

from ahub import drafts
from ahub.cliutil import CliError, add_project_arg, emit, resolve_project
from ahub.store import Store


def cmd_new(args) -> int:
    from ahub.i18n import t

    project = resolve_project(args)
    store = Store()
    did = drafts.create(store, project, args.text, source="cli")
    emit(args, {"id": did}, drafts.preview(store, did) + "\n\n" + t("draft.start_hint", id=did))
    return 0


def cmd_start(args) -> int:
    from ahub.i18n import t

    project = resolve_project(args)
    try:
        tid = drafts.start(Store(), project, args.id)
    except ValueError as e:
        raise CliError(str(e)) from e
    emit(args, {"task": tid}, t("draft.queued", tid=tid))
    return 0


def cmd_cancel(args) -> int:
    from ahub.i18n import t

    ok = drafts.cancel(Store(), args.id)
    emit(args, {"ok": ok}, t("draft.cancelled") if ok else t("draft.cancel_no"))
    return 0 if ok else 2


def cmd_list(args) -> int:
    from ahub.i18n import t

    rows = drafts.list_drafts(Store())
    emit(args, {"drafts": rows}, "\n".join(f"#{r['id']} {r['status']} {r['text'][:70]}" for r in rows) or t("draft.empty"))
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
