"""ahub projects — hub projects and their config problems."""

from __future__ import annotations

from dataclasses import asdict

from ahub import config, ui
from ahub.cliutil import emit


def cmd_projects(args) -> int:
    from ahub.i18n import t

    hub = config.load_hub()
    projects, errors = config.load_projects(hub)
    problems = {p.name: config.check_project(p) for p in projects}
    head = [t("projects.col_name"), t("projects.col_root"), t("projects.col_state")]
    rows = [[p.name, p.root, "—" if not problems[p.name]
             else t("projects.state_bad_one") if len(problems[p.name]) == 1
             else t("projects.state_bad", n=len(problems[p.name]))] for p in projects]
    out: list[str] = []
    if projects:
        table = ui.table(head, rows, max_width=[18, None, 20], indent=2).split("\n")
        out.append(table[0])
        for i, p in enumerate(projects, start=1):
            out.append(table[i])
            out.extend(ui.para(e, indent=4) for e in problems[p.name])
    else:
        out.append(ui.para(t("projects.empty", source=hub.source or config.paths.global_config_path()), indent=2))
    out.extend(ui.para(f"! {e}", indent=2) for e in errors)
    if not rows:
        out.append(ui.styled(ui.kv([(t("views.lbl_next"), t("projects.next", path="."))]), "dim"))
    emit(args, {"projects": [asdict(p) for p in projects], "problems": problems, "errors": errors},
         "\n".join(out))
    return 1 if errors or any(problems.values()) else 0


def register(subparsers) -> None:
    from ahub.i18n import t

    p = subparsers.add_parser("projects", help=t("help.projects"))
    p.set_defaults(func=cmd_projects)
