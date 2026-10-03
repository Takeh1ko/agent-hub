"""Shared by subcommands: text/JSON output, project pick, the scope guard of a single-task command,
the "what to do" hint of a refusal."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from ahub import config, scope
from ahub.store import Task

_TOKEN = re.compile(r"[\w:.,=<>/*-]+")
# the words a command never takes: the tail after them is prose, not part of the command
_PROSE = frozenset("""a an and as at be by for from in into is it its no not of on or only that the their them then to
with without again also because before after instead must should can will would could do does done give gives
holds look looks needs offer offers please print prints refuses run runs see see try use uses wait waits work
works you your""".split())


class CliError(RuntimeError):
    """Expected command refusal: printed on one line, exit 2 (a hint — when the error carries one — under it)."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


def command_hint(message: str, known: frozenset[str] = frozenset()) -> str:
    """The runnable `ahub …` command a refusal points at — the last one, ``ahub`` backticks dropped.

    A refusal often ends with the way out ("… is off (ahub providers enable codex)"); a hint must be a
    command a person can paste, so the tail after it is cut at the first word that cannot belong to a
    command: prose (`_PROSE`, unless it is a subcommand name) and anything that is not `ahub`.
    """
    found = ""
    for candidate in _candidates((message or "").replace("`", "")):
        parts = []
        for token in _TOKEN.findall(candidate):
            word = token.strip(".,")
            if not word or word in known or word not in _PROSE:
                parts.append(word)
                continue
            break  # prose — the command ends here
        if parts:
            found = " ".join(parts)
    return found


def _candidates(text: str) -> list[str]:
    """The `ahub …` runs of the text — `ahub` on its own word (never "the hub", never "ahub[telegram]")."""
    out = []
    for match in re.finditer(r"(?<![\w-])ahub(?=\s|$)", text):
        tail = text[match.end():]
        end = min([i for i in (tail.find(c) for c in "()\n;") if i > 0] or [len(tail)])
        out.append(("ahub" + tail[:end]).strip())
    return out


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

        raise CliError(t("err.no_project", want=want), hint=t("hint.projects"))
    try:
        return config.load_project(cwd)
    except FileNotFoundError:
        projects, _errors = config.load_projects()
        cfg = config.project_for(cwd, projects)
        if cfg is None:
            from ahub.i18n import t

            raise CliError(t("err.no_project_cwd", cwd=cwd, file=config.PROJECT_FILE),
                           hint=t("hint.setup")) from None
        return cfg
