"""Конфиг проекта (.hub.toml) и список проектов. Чистые функции чтения."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def _expand(value: str) -> str:
    """Развернуть $HOME/~ в пути."""
    return os.path.expandvars(os.path.expanduser(value))


@dataclass
class Hooks:
    task_setup: str = ""
    task_cleanup: str = ""


@dataclass
class Defaults:
    executor: str = "muse"
    reviewers: list[str] = field(default_factory=lambda: ["muse", "mimoflash"])
    budget_go: float = 0.5
    budget_usd: float = 0.0


@dataclass
class ProjectConfig:
    schema_version: int = 1
    name: str = ""
    root: str = ""
    worktrees: str = ""
    rules: str = ""
    python: str = ""
    test_lock: str = ""
    work_branch: str = ""
    push: str = ""
    allowed_paths: list[str] = field(default_factory=list)
    hooks: Hooks = field(default_factory=Hooks)
    defaults: Defaults = field(default_factory=Defaults)
    levels: dict[str, str] = field(default_factory=dict)
    idle_s: int = 900  # сторож тишины opencode: нет JSON-событий N c


def _idle_of(data: dict) -> int:
    """Порог тишины opencode: топ-level idle_s, иначе 900."""
    raw = data.get("idle_s", data.get("opencode_idle_s", 900))
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return 900
    return v if v >= 0 else 900


def _from_dict(data: dict) -> ProjectConfig:
    hooks = data.get("hooks", {}) or {}
    defaults = data.get("defaults", {}) or {}
    reviewers = defaults.get("reviewers", ["muse", "mimoflash"])
    if isinstance(reviewers, str):
        reviewers = [r.strip() for r in reviewers.split(",") if r.strip()]
    return ProjectConfig(
        schema_version=int(data.get("schema_version", 1)),
        name=str(data.get("name", "")),
        root=_expand(str(data.get("root", ""))),
        worktrees=_expand(str(data.get("worktrees", ""))),
        rules=_expand(str(data.get("rules", ""))),
        python=_expand(str(data.get("python", ""))),
        test_lock=_expand(str(data.get("test_lock", ""))),
        work_branch=str(data.get("work_branch", "")),
        push=str(data.get("push", "")),
        allowed_paths=[str(p) for p in (data.get("allowed_paths", []) or [])],
        hooks=Hooks(
            task_setup=str(hooks.get("task_setup", "")),
            task_cleanup=str(hooks.get("task_cleanup", "")),
        ),
        defaults=Defaults(
            executor=str(defaults.get("executor", "muse")),
            reviewers=list(reviewers),
            budget_go=float(defaults.get("budget_go", 0.5)),
            budget_usd=float(defaults.get("budget_usd", 0.0)),
        ),
        levels={str(k): str(v) for k, v in (data.get("levels", {}) or {}).items()},
        idle_s=_idle_of(data),
    )


def load_project(path: str | Path) -> ProjectConfig:
    """Найти .hub.toml вверх от path и прочитать."""
    cur = Path(path).resolve()
    if cur.is_file():
        cur = cur.parent
    for cand in [cur, *cur.parents]:
        toml = cand / ".hub.toml"
        if toml.is_file():
            return _from_dict(tomllib.loads(toml.read_text(encoding="utf-8")))
    raise FileNotFoundError(f"нет .hub.toml выше {path}")


def global_config_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent-hub" / "config.toml"


def load_projects(config_path: str | Path | None = None) -> list[ProjectConfig]:
    """Проекты из ~/.config/agent-hub/config.toml. Нет файла → []."""
    cfg = Path(config_path) if config_path is not None else global_config_path()
    if not cfg.is_file():
        return []
    try:
        data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    out: list[ProjectConfig] = []
    for entry in data.get("projects", []) or []:
        try:
            out.append(load_project(_expand(str(entry))))
        except (FileNotFoundError, OSError):
            continue
    return out
