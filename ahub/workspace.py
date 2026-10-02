"""Рабочая копия задачи: git worktree + ветка (contracts §1).

V08 — базовое создание/проверка/удаление. Копия без секретов, хуки проекта, проверка окружения — V12.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from ahub.config import ProjectConfig
from ahub.i18n import t as _t

AHUB_DIR = ".ahub"
EXCLUDES = (f"{AHUB_DIR}/", "__pycache__/", ".pytest_cache/")  # служебное — мимо git копии


class WorkspaceError(RuntimeError):
    pass


def git(cwd: str | Path, *args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[-500:]}")
    return r


@dataclass(frozen=True)
class Workspace:
    path: str
    branch: str
    base_sha: str


def worktree_path(project: ProjectConfig, task_id: int) -> Path:
    base = Path(project.worktrees) if project.worktrees else Path(project.root).parent / f"{Path(project.root).name}-wt"
    return base / f"T{task_id}"


def branch_name(project: ProjectConfig, task_id: int) -> str:
    return f"{project.branch_prefix}T{task_id}"


def _exclude_ahub(path: Path) -> None:
    """`.ahub/` и кэши Python не видны git'у копии (info/exclude общий для репозитория — это и нужно)."""
    r = git(path, "rev-parse", "--git-path", "info/exclude")
    excl = Path(r.stdout.strip())
    if not excl.is_absolute():
        excl = path / excl
    excl.parent.mkdir(parents=True, exist_ok=True)
    lines = excl.read_text(encoding="utf-8").splitlines() if excl.exists() else []
    missing = [x for x in EXCLUDES if x not in lines]
    if missing:
        with excl.open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(missing) + "\n")


def ensure(project: ProjectConfig, task_id: int, *, base_ref: str | None = None) -> Workspace:
    """Создать копию задачи или вернуть существующую (идемпотентно: продолжение, повтор после сбоя)."""
    path = worktree_path(project, task_id)
    branch = branch_name(project, task_id)
    root = project.root
    if path.is_dir() and (path / ".git").exists():
        cur = git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if cur != branch:
            raise WorkspaceError(_t("workspace.wrong_branch", path=path, cur=cur, branch=branch))
        base = git(root, "merge-base", project.work_branch, branch, check=False).stdout.strip()
        _exclude_ahub(path)
        (path / AHUB_DIR).mkdir(exist_ok=True)
        return Workspace(str(path), branch, base)
    ref = base_ref or project.work_branch
    base_sha = git(root, "rev-parse", ref).stdout.strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if exists:
        git(root, "worktree", "add", str(path), branch)
    else:
        git(root, "worktree", "add", "-b", branch, str(path), base_sha)
    _exclude_ahub(path)
    (path / AHUB_DIR).mkdir(exist_ok=True)
    return Workspace(str(path), branch, base_sha)


def changed_files(path: str | Path) -> list[str]:
    """Незакоммиченные изменения копии (без .ahub/)."""
    r = git(path, "status", "--porcelain", "--untracked-files=all")
    out = []
    for line in r.stdout.splitlines():
        name = line[3:].strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.strip('"')
        if not name.startswith(f"{AHUB_DIR}/"):
            out.append(name)
    return out


def head(path: str | Path) -> str:
    return git(path, "rev-parse", "HEAD").stdout.strip()


def commits_since(path: str | Path, base: str) -> int:
    r = git(path, "rev-list", "--count", f"{base}..HEAD")
    return int(r.stdout.strip() or 0)


def remove(project: ProjectConfig, task_id: int, *, delete_branch: bool = False) -> None:
    path = worktree_path(project, task_id)
    if path.exists():
        git(project.root, "worktree", "remove", "--force", str(path), check=False)
    git(project.root, "worktree", "prune", check=False)
    if delete_branch:
        git(project.root, "branch", "-D", branch_name(project, task_id), check=False)
