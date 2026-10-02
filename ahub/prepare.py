"""Подготовка задачи (V12, architecture §6.2): копия без секретов, хуки проекта, окружение, проверка приёмки.

Провал подготовки — «Ошибка» с причиной, модель не вызывается.
- Секреты: неотслеживаемые файлы (.env и т.п.) в git worktree не попадают сами; отслеживаемые файлы по списку
  `[secrets] exclude` проекта скрываются из копии (sparse-checkout, в индексе остаются — дифф их не видит).
- Окружение работника: из окружения хаба убираются токены/пароли/ключи (кроме нужных поставщику моделей).
- Хуки: shell-команды проекта в копии, env AHUB_TASK_ID / AHUB_WORKTREE / AHUB_PROJECT_ROOT.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ahub import workspace
from ahub.config import ProjectConfig
from ahub.i18n import t as _t
from ahub.store import Task

HOOK_TIMEOUT_S = 600
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|TELEGRAM|BOT_|API_KEY|_KEY$)",
                         re.IGNORECASE)
# Что поставщикам моделей нужно из окружения, даже если похоже на секрет.
KEEP_ENV = re.compile(r"^(OPENROUTER_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|GEMINI_API_KEY|GOOGLE_API_KEY|"
                      r"DEEPSEEK_API_KEY|OPENCODE_.*|HTTPS?_PROXY|NO_PROXY|ALL_PROXY)$", re.IGNORECASE)


class PrepareError(RuntimeError):
    pass


def task_env(label: str, worktree: str, root: str) -> dict[str, str]:
    """Переменные задачи для хуков и приёмки; HUB_* — совместимость с хуками проектов, писанными под v1."""
    return {"AHUB_TASK_ID": label, "AHUB_WORKTREE": worktree, "AHUB_PROJECT_ROOT": root,
            "HUB_TASK_ID": label, "HUB_WORKTREE": worktree, "HUB_PROJECT_ROOT": root}


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """Окружение без секретов хаба; ключи моделей и прокси остаются."""
    out = {}
    for k, v in env.items():
        if KEEP_ENV.match(k) or not _SECRET_ENV.search(k):
            out[k] = v
    return out


def hide_secrets(project: ProjectConfig, path: str) -> list[str]:
    """Скрыть из копии отслеживаемые файлы по списку исключений проекта. Возвращает скрытые."""
    tracked = workspace.git(path, "ls-files").stdout.splitlines()
    hidden = [f for f in tracked
              if any(fnmatch.fnmatch(f, pat) or fnmatch.fnmatch(Path(f).name, pat) for pat in project.secret_excludes)]
    if not hidden:
        return []
    workspace.git(path, "sparse-checkout", "init", "--no-cone")
    patterns = ["/*"] + [f"!/{f}" for f in hidden]
    workspace.git(path, "sparse-checkout", "set", "--no-cone", *patterns)
    return hidden


def run_hook(project: ProjectConfig, name: str, task: Task, worktree: str) -> None:
    cmd = getattr(project.hooks, name, "") or ""
    if not cmd.strip():
        return
    env = scrub_env(dict(os.environ))
    env.update(task_env(task.label, worktree, project.root))
    try:
        r = subprocess.run(cmd, shell=True, cwd=worktree, env=env, capture_output=True, text=True,
                           timeout=HOOK_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise PrepareError(_t("prepare.hook_timeout", name=name, timeout=HOOK_TIMEOUT_S)) from e
    if r.returncode != 0:
        tail = (r.stdout + "\n" + r.stderr).strip()[-600:]
        raise PrepareError(_t("prepare.hook_failed", name=name, code=r.returncode, tail=tail))


def collect(project: ProjectConfig, worktree: str, nodes: list[str]) -> None:
    """Приёмка собирается в копии (существующие файлы; новые напишет работник)."""
    existing = [n for n in nodes if (Path(worktree) / n.split("::")[0]).exists()]
    if not existing:
        return
    py = project.python or "python3"
    try:
        r = subprocess.run([py, "-m", "pytest", "--collect-only", "-q", *existing], cwd=worktree,
                           capture_output=True, text=True, timeout=300,
                           env={**scrub_env(dict(os.environ)), "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        raise PrepareError(_t("prepare.collect_error", err=e)) from e
    if r.returncode != 0:
        last = (r.stdout + "\n" + r.stderr).strip().splitlines()[-1:] or [_t("err.exit_code", code=r.returncode)]
        raise PrepareError(_t("prepare.collect_error", err=last[0][-300:]))


@dataclass(frozen=True)
class Prepared:
    workspace: workspace.Workspace
    hidden: list[str]


def prepare(project: ProjectConfig, task: Task, *, base_ref: str | None = None) -> Prepared:
    """Копия, скрытые секреты, хук task_setup, сбор приёмки. Идемпотентно (продолжение после сбоя)."""
    ws = workspace.ensure(project, task.id, base_ref=base_ref)
    hidden = hide_secrets(project, ws.path)
    run_hook(project, "task_setup", task, ws.path)
    if task.limits.get("accept"):
        collect(project, ws.path, list(task.limits["accept"]))
    return Prepared(ws, hidden)
