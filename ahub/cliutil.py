"""Shared by subcommands: text/JSON output, project pick, the "what to do" hint of a refusal."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from ahub import config

_COMMAND = re.compile(r"(?:^|[\s(])(ahub (?:[a-z][\w:.-]*)(?: [^\s(),;]+)*)")


class CliError(RuntimeError):
    """Expected command refusal: `error: …` on one line, exit 2.

    hint — the exact command to run; without it the command is taken from the message itself (a
    registry refusal names it in parentheses), so the second line is printed whenever one is known.
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def command_hint(message: str) -> str:
    """The `ahub …` command a refusal points at (its last one), '' — nothing obvious."""
    found = _COMMAND.findall(message or "")
    return found[-1].strip().rstrip(".,") if found else ""


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
    from ahub.i18n import t

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
        raise CliError(t("err.no_project", want=want), hint=t("hint.projects"))
    try:
        return config.load_project(cwd)
    except FileNotFoundError:
        projects, _errors = config.load_projects()
        cfg = config.project_for(cwd, projects)
        if cfg is None:
            raise CliError(t("err.no_project_cwd", cwd=cwd, file=config.PROJECT_FILE),
                           hint=t("hint.setup")) from None
        return cfg
