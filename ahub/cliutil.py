"""Shared by subcommands: text/JSON output, project pick."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ahub import config


class CliError(RuntimeError):
    """Expected command refusal: printed on one line, exit 2."""


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
