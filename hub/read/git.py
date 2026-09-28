"""Git-слой: коммиты, дифф, грязь, worktrees. Только subprocess, короткие вызовы."""

from __future__ import annotations

import subprocess


def _run(*args: str, cwd: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(args), cwd=cwd, capture_output=True, text=True, timeout=60,
        )
    except OSError as e:
        return subprocess.CompletedProcess(
            args=list(args), returncode=127, stdout="", stderr=str(e))


def branch_commits(repo: str, base: str, branch: str) -> int:
    """Число коммитов base..branch."""
    r = _run("git", "rev-list", "--count", f"{base}..{branch}", "--", cwd=repo)
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def diff_stat(repo: str, base: str, head: str) -> str:
    """Краткий diff --stat base..head."""
    r = _run("git", "diff", "--stat", f"{base}..{head}", "--", cwd=repo)
    return r.stdout.strip() if r.returncode == 0 else ""


def is_dirty(worktree: str) -> bool:
    """Есть ли незакоммиченные изменения.

    Каталог без .git или ошибка git — считаем «грязным»,
    чтобы ворота падали, а не пропускали ложно-зелёным.
    """
    r = _run("git", "status", "--porcelain", cwd=worktree)
    if r.returncode != 0:
        return True
    return bool(r.stdout.strip())


def worktrees(repo: str) -> list[dict]:
    """Список worktrees (git worktree list --porcelain)."""
    r = _run("git", "worktree", "list", "--porcelain", cwd=repo)
    if r.returncode != 0:
        return []
    out: list[dict] = []
    cur: dict = {}
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].strip()
        elif not line.strip():
            if cur:
                out.append(cur)
                cur = {}
    if cur:
        out.append(cur)
    return out
