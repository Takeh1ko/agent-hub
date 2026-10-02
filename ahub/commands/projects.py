"""ahub projects — hub projects and their config problems."""

from __future__ import annotations

from dataclasses import asdict

from ahub import config
from ahub.cliutil import emit


def cmd_projects(args) -> int:
    from ahub.i18n import t

    hub = config.load_hub()
    projects, errors = config.load_projects(hub)
    problems = {p.name: config.check_project(p) for p in projects}
    lines = []
    for p in projects:
        mark = "!" if problems[p.name] else " "
        lines.append(f"{mark} {p.name:<16} {p.root}")
        lines.extend(f"    {e}" for e in problems[p.name])
    lines.extend(f"! {e}" for e in errors)
    if not lines:
        lines.append(t("projects.empty", source=hub.source or config.paths.global_config_path()))
    emit(args, {"projects": [asdict(p) for p in projects], "problems": problems, "errors": errors},
         "\n".join(lines))
    return 1 if errors or any(problems.values()) else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("projects", help=t("help.projects"))
    p.set_defaults(func=cmd_projects)
