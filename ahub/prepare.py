"""Task preparation (V12, architecture §6.2): secret-free copy, project hooks, env, acceptance check.

Failed preparation — "Error" with a reason, the model is never called.
- Secrets: untracked files (.env etc.) never land in a git worktree on their own; tracked files on the project's
  `[secrets] exclude` list stay out of the copy (sparse-checkout, remain indexed — diff never sees them).
- Worker env: hub tokens/passwords/keys stripped (except what the model providers need), and the provider's own proxy
  from `[providers.<name>]` in the hub config (see apply_proxy).
- Hooks: project shell commands in the copy, env AHUB_TASK_ID / AHUB_WORKTREE / AHUB_PROJECT_ROOT.
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
_PROXY_URL_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")  # what a proxy URL goes into
_NO_PROXY_VARS = ("NO_PROXY",)
_SECRET_ENV = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|TELEGRAM|BOT_|API_KEY|_KEY$)",
                         re.IGNORECASE)
# The provider API keys the hub keeps, each with the provider id opencode knows it by (`ahub doctor`
# checks that a key of this env reaches the service).
PROVIDER_KEYS: dict[str, str] = {
    "OPENROUTER_API_KEY": "openrouter",
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "GEMINI_API_KEY": "google",
    "GOOGLE_API_KEY": "google",
    "DEEPSEEK_API_KEY": "deepseek",
}
# What model providers need from the env, even if it looks like a secret.
KEEP_ENV = re.compile(rf"^({'|'.join(PROVIDER_KEYS)}|OPENCODE_.*|HTTPS?_PROXY|NO_PROXY|ALL_PROXY)$",
                      re.IGNORECASE)


class PrepareError(RuntimeError):
    pass


def task_env(label: str, worktree: str, root: str) -> dict[str, str]:
    """Task variables for hooks and acceptance; HUB_* — compat with project hooks written for v1."""
    return {"AHUB_TASK_ID": label, "AHUB_WORKTREE": worktree, "AHUB_PROJECT_ROOT": root,
            "HUB_TASK_ID": label, "HUB_WORKTREE": worktree, "HUB_PROJECT_ROOT": root}


def scrub_env(env: dict[str, str]) -> dict[str, str]:
    """Env without hub secrets; model keys and proxies stay."""
    out = {}
    for k, v in env.items():
        if KEEP_ENV.match(k) or not _SECRET_ENV.search(k):
            out[k] = v
    return out


def apply_proxy(env: dict[str, str], proxy: str | None, no_proxy: str | None = None) -> dict[str, str]:
    """Provider process env from its [providers.<name>] section, per key.

    A key is absent (None) — the inherited variables stay as they are; "" — explicitly none, they are
    dropped; a value — set (proxy: HTTPS_PROXY/HTTP_PROXY/ALL_PROXY and the lowercase ones, no_proxy:
    NO_PROXY/no_proxy). Both letter cases are set and dropped.
    """
    if proxy is None and no_proxy is None:
        return dict(env)
    out = dict(env)
    if proxy is not None:
        out = {k: v for k, v in out.items() if k.upper() not in _PROXY_URL_VARS}
        if proxy:
            for name in _PROXY_URL_VARS:
                out[name] = out[name.lower()] = proxy
    if no_proxy is not None:
        out = {k: v for k, v in out.items() if k.upper() not in _NO_PROXY_VARS}
        if no_proxy:
            for name in _NO_PROXY_VARS:
                out[name] = out[name.lower()] = no_proxy
    return out


def hide_secrets(project: ProjectConfig, path: str) -> list[str]:
    """Hide tracked files in the copy per the project exclude list. Returns the hidden ones."""
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
    except OSError as e:  # the copy is gone (an accept resumed after the cleanup)
        raise PrepareError(_t("prepare.hook_start", name=name, err=e)) from e
    if r.returncode != 0:
        tail = (r.stdout + "\n" + r.stderr).strip()[-600:]
        raise PrepareError(_t("prepare.hook_failed", name=name, code=r.returncode, tail=tail))


def collect(project: ProjectConfig, worktree: str, nodes: list[str]) -> None:
    """Acceptance collects in the copy (existing files; the worker writes new ones)."""
    existing = [n for n in nodes if (Path(worktree) / n.split("::")[0]).exists()]
    if not existing:
        return
    py = project.python_bin()
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
    """Copy, hidden secrets, task_setup hook, acceptance collect. Idempotent (resume after failure)."""
    ws = workspace.ensure(project, task.id, base_ref=base_ref)
    hidden = hide_secrets(project, ws.path)
    run_hook(project, "task_setup", task, ws.path)
    if task.limits.get("accept"):
        collect(project, ws.path, list(task.limits["accept"]))
    return Prepared(ws, hidden)
