"""Per-project scope of a handle: one hub, one database, several repositories (architecture §9).

A Claude session works in one repository and sees and touches only that project; the owner sees everything.
`resolve(args)` is the one place the scope of a command comes from: `--all` — every project (the owner),
`--project X` — X, otherwise the project of the current directory (`.hub.toml` searched upward); outside every
project — every project. Rows with project = '' (hub-wide: observer alarms, service events) belong to everyone,
so `where()` adds `project IN (…) OR project=''`.
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


def where(scope: Scope | None, column: str = "project") -> tuple[str, list[str]]:
    """The scope as a SQL condition: `column IN (…) OR column=''`; the owner (or None) — no condition."""
    if scope is None or scope.all:
        return "", []
    marks = ",".join("?" * len(scope.projects))
    return f"({column} IN ({marks}) OR {column}='')", list(scope.projects)


def resolve(args: Any = None, cwd: str | Path | None = None) -> Scope:
    """The scope of a command: `--all` — every project, `--project X` — X, else the project of the directory."""
    if getattr(args, "all", False):
        return Scope()
    want = getattr(args, "project", None)
    if want:
        return Scope((name_of(want),))
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
