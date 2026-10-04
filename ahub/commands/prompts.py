"""ahub prompts: inspect, show, edit, and check prompt guidance files."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from ahub import gates, paths, prompts, review, ui
from ahub.cliutil import CliError, add_project_arg, emit, resolve_project
from ahub.i18n import t
from ahub.model import Kind
from ahub.store import Task


def _short_path(path: Path, project_root: str | None = None) -> str:
    """Format path for compact CLI display."""
    sp = str(path)
    if project_root:
        try:
            rel = path.relative_to(project_root)
            return str(rel)
        except ValueError:
            pass
    home = str(Path.home())
    if sp.startswith(home):
        return "~" + sp[len(home):]
    return sp


def _format_size(size: int) -> str:
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _dummy_task(project_name: str, role: str) -> Task:
    kind = {
        "scout": Kind.SCOUT,
        "code": Kind.CODE,
        "routine": Kind.ROUTINE,
        "review": Kind.REVIEW,
        "all": Kind.CODE,
    }[role]
    limits: dict = {
        Kind.CODE: {"paths": ["src/**"], "accept": ["tests/test_example.py"]},
        Kind.ROUTINE: {"paths": ["docs/**"], "accept": []},
        Kind.SCOUT: {"read": ["README.md"]},
        Kind.REVIEW: {"input": "src/**"},
    }[kind]
    return Task(
        id=0,
        project=project_name,
        kind=kind,
        title="Example task",
        spec="Task specification.",
        limits=limits,
    )


def cmd_prompts(args) -> int:
    """Default: show prompt layers that apply to the current project."""
    project = resolve_project(args)
    roles_data = {}
    rows = []
    empty_roles = []

    for r in prompts.ROLES:
        # Check global
        g_path = paths.global_prompts_dir() / f"{r}.md"
        g_exists = g_path.is_file()
        g_size = g_path.stat().st_size if g_exists else 0

        # Check project
        if r == "all":
            p_path, _ = prompts.resolve_project_all_file(project)
            p_target = p_path if p_path is not None else paths.project_prompts_dir(project.root) / "all.md"
        else:
            p_target = paths.project_prompts_dir(project.root) / f"{r}.md"
        p_exists = p_target.is_file()
        p_size = p_target.stat().st_size if p_exists else 0

        # Check local
        l_path = paths.local_prompts_dir(project.name) / f"{r}.md"
        l_exists = l_path.is_file()
        l_size = l_path.stat().st_size if l_exists else 0

        roles_data[r] = {
            "global": {"path": str(g_path), "size": g_size, "exists": g_exists},
            "project": {"path": str(p_target), "size": p_size, "exists": p_exists},
            "local": {"path": str(l_path), "size": l_size, "exists": l_exists},
        }

        g_cell = f"{_short_path(g_path)} ({_format_size(g_size)})" if g_exists else "—"
        p_cell = f"{_short_path(p_target, project.root)} ({_format_size(p_size)})" if p_exists else "—"
        l_cell = f"{_short_path(l_path)} ({_format_size(l_size)})" if l_exists else "—"

        is_empty = not (g_exists or p_exists or l_exists)
        if is_empty:
            empty_roles.append(r)

        rows.append((r, [r, g_cell, p_cell, l_cell], is_empty))

    head = [t("prompts.col_role"), t("prompts.col_global"), t("prompts.col_project"), t("prompts.col_local")]
    table_lines = ui.table(head, [cells for _, cells, _ in rows], indent=2).splitlines()
    out = [table_lines[0]]  # header
    for i, (r, _cells, is_empty) in enumerate(rows, start=1):
        out.append(table_lines[i])
        if is_empty:
            hint_str = t("prompts.hint_empty", role=r)
            out.append(ui.hint(hint_str, indent=4) if ui.colour_on() else f"    → {hint_str}")

    emit(args, {"project": project.name, "roles": roles_data}, "\n".join(out))
    return 0


def cmd_show(args) -> int:
    role = args.role
    if role not in prompts.ROLES:
        raise CliError(t("err.unknown_role", role=role, known=", ".join(prompts.ROLES)))

    project = resolve_project(args)
    task = _dummy_task(project.name, role)

    if role == "scout":
        prompt_text = prompts.scout_prompt(project, task)
    elif role in ("code", "routine"):
        prompt_text = prompts.code_prompt(project, task)
    elif role == "review":
        dummy_gate = gates.GateResult(base="base", head="head", diffstat="1 file changed")
        dummy_diff = "diff --git a/file.py b/file.py\n--- a/file.py\n+++ b/file.py\n@@ -1 +1 @@\n-old\n+new\n"
        prompt_text = review.review_prompt(project, task, dummy_diff, dummy_gate, 1, "reviewer")
    else:  # "all"
        sections, _, _ = prompts.assemble_guidance(project, "all")
        prompt_text = "\n\n".join([*sections, prompts._header(task), prompts.code_delivery(task)])

    _, summary, layers = prompts.assemble_guidance(project, role)

    data = {
        "role": role,
        "summary": summary,
        "guidance": [
            {"scope": layer.scope, "role": layer.role, "path": str(layer.path),
             "heading": layer.heading, "content": layer.content}
            for layer in layers if layer.exists
        ],
        "prompt": prompt_text,
    }
    emit(args, data, prompt_text)
    return 0


def cmd_edit(args) -> int:
    role = args.role
    if role not in prompts.ROLES:
        raise CliError(t("err.unknown_role", role=role, known=", ".join(prompts.ROLES)))

    if args.is_global:
        scope_name = "global"
        target = paths.global_prompts_dir() / f"{role}.md"
    elif args.is_local:
        scope_name = "local"
        project = resolve_project(args)
        target = paths.local_prompts_dir(project.name) / f"{role}.md"
    else:
        scope_name = "project"
        project = resolve_project(args)
        target = paths.project_prompts_dir(project.root) / f"{role}.md"

    if not target.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        template = (
            f"<!-- Guidance for {role} ({scope_name}) -->\n"
            "<!-- Rules here are loaded before the task spec and the built-in hub layer. -->\n"
        )
        target.write_text(template, encoding="utf-8")

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        try:
            res = subprocess.run([*shlex.split(editor), str(target)])
            return res.returncode
        except OSError:
            pass

    emit(args, {"path": str(target)}, str(target))
    return 0


def cmd_check(args) -> int:
    project = None
    try:
        project = resolve_project(args)
    except Exception:
        pass

    issues = prompts.check_prompts_for_project(project)
    has_errors = any(i.severity == "error" for i in issues)

    out = []
    if not issues:
        ok_msg = t("prompts.check_ok")
        out.append(ui.item(ok_msg) if ui.colour_on() else ok_msg)
    else:
        for i in issues:
            if i.severity == "error":
                mark = ui.styled("✗", "red")
                out.append(f"  {mark} {i.message}")
            elif i.severity == "warning":
                mark = ui.styled("!", "yellow")
                out.append(f"  {mark} {i.message}")
            else:
                mark = ui.styled("→", "dim")
                out.append(f"  {mark} {i.message}")

    data = {
        "ok": not has_errors,
        "issues": [{"path": str(i.path) if i.path else None, "severity": i.severity, "message": i.message}
                   for i in issues],
    }
    emit(args, data, "\n".join(out))
    return 1 if has_errors else 0


def register(subparsers) -> None:
    p = subparsers.add_parser("prompts", help=t("help.prompts"))
    add_project_arg(p)
    p.set_defaults(func=cmd_prompts)

    sub = p.add_subparsers(dest="prompts_cmd")

    # show
    s = sub.add_parser("show", help=t("help.prompts_show"))
    s.add_argument("role", choices=prompts.ROLES, help=t("help.prompts_role"))
    add_project_arg(s)
    s.set_defaults(func=cmd_show)

    # edit
    e = sub.add_parser("edit", help=t("help.prompts_edit"))
    e.add_argument("role", choices=prompts.ROLES, help=t("help.prompts_role"))
    grp = e.add_mutually_exclusive_group()
    grp.add_argument("--global", dest="is_global", action="store_true", help=t("help.prompts_global"))
    grp.add_argument("--local", dest="is_local", action="store_true", help=t("help.prompts_local"))
    add_project_arg(e)
    e.set_defaults(func=cmd_edit)

    # check
    c = sub.add_parser("check", help=t("help.prompts_check"))
    add_project_arg(c)
    c.set_defaults(func=cmd_check)
