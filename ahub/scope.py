"""Per-project scope of a handle: one hub, one database, several repositories (architecture §9).

A Claude session works in one repository and sees and touches only that project; the owner sees everything.
`resolve(args)` is the one place the scope of a command comes from: `--all` — every project (the owner),
`--project X` — X, otherwise the project of the current directory (`.hub.toml` searched upward); outside every
project — every project. `--project X` is checked against the hub (`checked`): an unknown name is a refusal with
the way out, never a silent empty scope — known are the names of the hub config, a path to a repository, and the
project of the directory itself (what the command resolves without the flag). Rows with project = '' (hub-wide:
observer alarms, service events) belong to everyone, so `where()` adds `project IN (…) OR project=''`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahub import config


@dataclass(frozen=True)
class Scope:
    """What a handle may see: one project, several, or everything (the owner)."""

    projects: tuple[str, ...] = ()  # empty — every project

    @property
    def all(self) -> bool:
        return not self.projects

    @property
    def name(self) -> str:
        """The project of the scope — what to write on a row ('' — hub-wide)."""
        return self.projects[0] if len(self.projects) == 1 else ""

    def __contains__(self, project: str) -> bool:
        """A row belongs to the scope: of its project, or hub-wide ('')."""
        return self.all or project == "" or project in self.projects


OWNER = Scope()  # the owner's scope — every project


def foreign(sc: Scope, project: str) -> bool:
    """True when a row of that project is outside the scope of the handle (architecture §9):
    a task of another project is refused — unless the scope is every project (--all)."""
    return not sc.all and project not in sc


def where(scope: Scope | None) -> tuple[str, list[str]]:
    """The scope as a SQL condition on the `project` column: `project IN (…) OR project=''`;
    the owner (or None) — no condition."""
    if scope is None or scope.all:
        return "", []
    marks = ",".join("?" * len(scope.projects))
    return f"(project IN ({marks}) OR project='')", list(scope.projects)


def resolve(args: Any = None, cwd: str | Path | None = None) -> Scope:
    """The scope of a command: `--all` — every project, `--project X` — X, else the project of the directory."""
    if getattr(args, "all", False):
        return Scope()
    want = getattr(args, "project", None)
    if want:
        return Scope((checked(want, cwd),))
    return of_dir(Path(cwd) if cwd is not None else Path.cwd())


def of_dir(start: str | Path) -> Scope:
    """The scope of a directory: its project; outside every project — every project.

    A broken .hub.toml is not a crash here: a read command must still work, so the scope is every project
    and the file itself is reported by `ahub projects`.
    """
    try:
        return Scope((config.load_project(start).name,))
    except (FileNotFoundError, config.ConfigError):
        return Scope()


def name_of(want: str) -> str:
    """`--project X`: a path to a repository (its .hub.toml gives the name) or a project name as typed."""
    p = Path(config.expand(want))
    if p.is_dir():
        try:
            return config.load_project(p).name
        except (FileNotFoundError, config.ConfigError):
            pass
    return want


def hub_names() -> list[str] | None:
    """The projects of the hub config; None — there is no config to ask (a broken one: a read command
    must still work, and `ahub projects` reports the file)."""
    try:
        projects, _errors = config.load_projects()
    except (config.ConfigError, OSError):
        return None
    return [p.name for p in projects]


def checked(want: str, cwd: str | Path | None = None) -> str:
    """`--project X`: the name of a project of this hub. An unknown one is a refusal, not a silent empty
    scope — the rows a command writes would carry that name and no handle would ever read them again.

    Known: a name of the hub config, a path to a repository of its own, and the project of the directory
    the command runs in — that one is what the same command resolves without the flag, so a hub that
    configures nothing (`ahub setup` not run yet, or a repo outside the config) still works. A typo of
    that name is not the name of the directory, and stays a refusal.
    """
    name = name_of(want)
    names = hub_names()
    if names is None or name in names or name == here_project(cwd) or _is_repo(want):
        return name
    from ahub.cliutil import CliError
    from ahub.i18n import t

    raise CliError(t("err.unknown_project", want=name, projects=", ".join(names) or "—"),
                   hint=t("hint.projects"))


def here_project(cwd: str | Path | None = None) -> str:
    """The project of the directory the command runs in — the scope it would take without `--project`."""
    return of_dir(Path(cwd) if cwd is not None else Path.cwd()).name


def _is_repo(want: str) -> bool:
    """A path that leads to a repository of its own — such a project is named by its .hub.toml."""
    p = Path(config.expand(want))
    if not p.is_dir():
        return False
    try:
        config.load_project(p)
    except (FileNotFoundError, config.ConfigError):
        return False
    return True
