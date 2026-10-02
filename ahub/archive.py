"""Task archive inside the project (architecture §12): `<project>/.agent-hub/` — local, not in project git.

tasks.md — shared list; tasks/T<id>/ — brief, worker result, report, diff (for code), cost, outcome.
The hub only writes; a human cleans up. Archive errors never break work — just a log line.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ahub import log as hublog
from ahub import workspace
from ahub.config import ProjectConfig
from ahub.i18n import Words
from ahub.i18n import t as _t
from ahub.model import CHANGES_FILES
from ahub.store import Store, Task
from ahub.time import fmt_local

DIR = ".agent-hub"
STATE_WORDS: Words = Words("archive.state_", ("draft", "queued", "preparing", "working", "checking",
                                             "reviewing", "fixing", "done", "needs_decision", "error",
                                             "stopped", "accepting", "accepted", "rejected"))
_log = hublog.get("archive")


def root(project: ProjectConfig) -> Path:
    return Path(project.root) / DIR


def _exclude(project: ProjectConfig) -> None:
    try:
        r = workspace.git(project.root, "rev-parse", "--git-path", "info/exclude", check=False)
        if r.returncode != 0:
            return
        excl = Path(r.stdout.strip())
        if not excl.is_absolute():
            excl = Path(project.root) / excl
        excl.parent.mkdir(parents=True, exist_ok=True)
        lines = excl.read_text(encoding="utf-8").splitlines() if excl.exists() else []
        if f"{DIR}/" not in lines:
            with excl.open("a", encoding="utf-8") as f:
                f.write(f"\n{DIR}/\n")
    except (OSError, workspace.WorkspaceError):
        pass


def task_cost(store: Store, task_id: int) -> tuple[float, float]:
    go = usd = 0.0
    for s in store.list_sessions(task_id):
        go += s.cost_go or 0.0
        usd += s.cost_usd or 0.0
    return go, usd


def _task_md(store: Store, t: Task) -> str:
    go, usd = task_cost(store, t.id)
    sessions = store.list_sessions(t.id)
    lines = [f"# {t.label} — {t.title}", "",
             _t("archive.type", kind=t.kind.value, state=STATE_WORDS.get(t.state.value, t.state.value))
             + (f" — {t.state_reason}" if t.state_reason else ""),
             _t("archive.model", executor=t.executor,
               review=', '.join(t.review.get('models', [])) or _t("archive.no_review"))
             + (f" × {t.review.get('rounds')}" if t.review else ""),
             _t("archive.rounds", round=t.round, sessions=len(sessions)),
             _t("archive.cost", go=f"{go:.3f}") + (_t("archive.cost_real", usd=f"{usd:.3f}") if usd else ""),
             _t("archive.created", when=fmt_local(t.created_at), by=t.created_by or '—')
             + (_t("archive.finished", when=fmt_local(t.finished_at)) if t.finished_at else ""),
             ""]
    if t.after:
        lines.insert(-1, _t("archive.after", items=', '.join(f'T{a}' for a in t.after)))
    if t.limits.get("paths"):
        lines.insert(-1, _t("archive.paths", items=', '.join(t.limits['paths'])))
    if t.limits.get("accept"):
        lines.insert(-1, _t("archive.accept", items=', '.join(t.limits['accept'])))
    if t.spec.strip():
        lines += [_t("archive.spec"), "", t.spec.strip(), ""]
    return "\n".join(lines)


def write_task(store: Store, project: ProjectConfig, task_id: int) -> Path | None:
    try:
        t = store.get_task(task_id)
        if t is None:
            return None
        base = root(project)
        d = base / "tasks" / t.label
        d.mkdir(parents=True, exist_ok=True)
        _exclude(project)
        (d / "task.md").write_text(_task_md(store, t), encoding="utf-8")
        if t.worktree and Path(t.worktree).is_dir():
            src = Path(t.worktree) / workspace.AHUB_DIR
            for name in ("result.json", "report.md"):
                if (src / name).is_file():
                    shutil.copyfile(src / name, d / name)
            for rv in src.glob("review_r*.json"):
                shutil.copyfile(rv, d / rv.name)
            if t.kind in CHANGES_FILES and t.base_sha:
                r = workspace.git(t.worktree, "diff", f"{t.base_sha}..HEAD", check=False)
                if r.returncode == 0:
                    (d / "diff.patch").write_text(r.stdout, encoding="utf-8")
        write_index(store, project)
        return d
    except Exception:
        _log.exception("archive T%s not written", task_id, extra={"task": task_id})
        return None


def write_index(store: Store, project: ProjectConfig) -> None:
    tasks = store.list_tasks(project=project.name, newest_first=True)
    rows = [_t("archive.index_title"), "", _t("archive.index_head"), "|---|---|---|---|---|---|"]
    for t in tasks:
        go, usd = task_cost(store, t.id)
        title = t.title.replace("|", "/")[:80]
        rows.append(f"| [{t.label}](tasks/{t.label}/task.md) | {fmt_local(t.created_at)} | {t.kind.value} | {title} |"
                    f" {STATE_WORDS.get(t.state.value, t.state.value)} | {go + usd:.3f} |")
    base = root(project)
    base.mkdir(parents=True, exist_ok=True)
    (base / "tasks.md").write_text("\n".join(rows) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
