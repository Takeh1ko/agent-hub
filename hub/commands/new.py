"""hub new: задача от владельца текстом — черновик модели, запуск по кнопке."""

from __future__ import annotations

import tomllib

from hub.config import load_project, load_projects
from hub.store import Store


def _resolve_project(name_or_path: str | None):
    if name_or_path:
        want = str(name_or_path).strip()
        for p in load_projects():
            if str(getattr(p, "name", "") or "") == want:
                return p
        return load_project(want)
    return load_project(".")


def cmd_new(args) -> int:
    store = Store()
    if getattr(args, "list", False):
        rows = store.list_drafts()
        if not rows:
            print("(черновиков нет)")
            return 0
        for r in rows:
            print(f"{r['id']} [{r.get('status')}] {r.get('project')}: "
                  f"{str(r.get('text') or '')[:80]}")
        return 0
    if getattr(args, "start", None) is not None:
        from hub.pipeline.draft import start_draft

        try:
            task_id = start_draft(store, int(args.start))
        except (ValueError, OSError) as e:
            print(f"не запустился: {e}")
            return 1
        print(f"OK {task_id}")
        return 0
    if getattr(args, "cancel", None) is not None:
        from hub.pipeline.draft import cancel_draft

        try:
            cancel_draft(store, int(args.cancel))
        except (ValueError, OSError) as e:
            print(f"не отменился: {e}")
            return 1
        print(f"отменён {args.cancel}")
        return 0
    text = " ".join(getattr(args, "text", None) or []).strip()
    if not text:
        print("нужен текст: hub new --project <имя> \"текст\"")
        return 2
    try:
        project = _resolve_project(getattr(args, "project", None))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"нет проекта: {e}")
        return 1
    from hub.pipeline.draft import draft_preview, make_draft, make_runner_for_project

    try:
        runner = make_runner_for_project(project)
    except (ValueError, OSError) as e:
        print(f"нет раннера: {e}")
        return 1
    try:
        draft_id = make_draft(store, project, text, "cli", runner)
    except (ValueError, OSError) as e:
        print(f"не создался: {e}")
        return 1
    row = store.get_draft(draft_id)
    status = str((row or {}).get("status") or "")
    if status == "ready":
        print(draft_preview(str((row or {}).get("card_text") or "")))
        print(f"черновик {draft_id} готов: hub new --start {draft_id}")
        return 0
    print(f"черновик {draft_id} не готов ({status}):")
    for line in str((row or {}).get("lint_errors") or "").splitlines()[:10]:
        print(line)
    return 1


def register(subparsers) -> None:
    p = subparsers.add_parser("new", help="задача от владельца текстом")
    p.add_argument("--project", default=None, help="имя проекта или путь")
    p.add_argument("--start", default=None, help="запустить черновик ID")
    p.add_argument("--cancel", default=None, help="отменить черновик ID")
    p.add_argument("--list", action="store_true", help="список черновиков")
    p.add_argument("text", nargs="*", help="текст задачи")
    p.set_defaults(func=cmd_new)
