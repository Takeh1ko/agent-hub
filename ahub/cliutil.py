"""Shared by subcommands: text/JSON output, project pick, the scope guard of a single-task command."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ahub import config, scope
from ahub.store import Task


class CliError(RuntimeError):
    """Expected command refusal: printed on one line, exit 2 (a hint — when the error carries one — under it)."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def emit(args, data: Any, text: str) -> None:
    """--json → data as JSON, else text."""
    if getattr(args, "json", False):
        json.dump(data, sys.stdout, ensure_ascii=False, separators=(",", ":"), default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")


def add_project_arg(parser) -> None:
    from ahub.i18n import t

    parser.add_argument("--project", "-P", default=None, help=t("cli.help_project"))


def add_scope_args(parser) -> None:
    """--project/--all — the scope of a handle (ahub/scope.py): the project of the current directory by
    default, --all — every project (the owner)."""
    from ahub.i18n import t

    parser.add_argument("--project", "-P", default=None, help=t("cli.help_project"))
    parser.add_argument("--all", action="store_true", help=t("cli.help_all"))


def check_task(args, task: Task) -> None:
    """A command that names one task belongs to that task's project (architecture §9).

    A task of another project is refused with the way out; `--all` or a matching `--project` opens the scope.
    """
    sc = scope.resolve(args)
    if not scope.foreign(sc, task.project):
        return
    from ahub.i18n import t

    raise CliError(t("err.foreign_task", label=task.label, project=task.project),
                   t("err.foreign_task_hint", project=task.project))


def resolve_project(args, cwd: str | Path | None = None) -> config.ProjectConfig:
    """Project by --project (hub config name or path) or by current dir."""
    want = getattr(args, "project", None)
    cwd = Path(cwd) if cwd is not None else Path.cwd()
    if want:
        p = Path(config.expand(want))
        if p.exists():
            return config.load_project(p)
        projects, _errors = config.load_projects()
        for cfg in projects:
            if cfg.name == want:
                return cfg
        from ahub.i18n import t

        raise CliError(t("err.no_project", want=want))
    try:
        return config.load_project(cwd)
    except FileNotFoundError:
        projects, _errors = config.load_projects()
        cfg = config.project_for(cwd, projects)
        if cfg is None:
            from ahub.i18n import t

            raise CliError(t("err.no_project_cwd", cwd=cwd, file=config.PROJECT_FILE)) from None
        return cfg
