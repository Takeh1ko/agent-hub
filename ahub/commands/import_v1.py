"""ahub import-v1 — список задач старого хаба (v1) в архив проекта: `<проект>/.agent-hub/v1-tasks.md` (V31a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ahub import archive, config
from ahub.cliutil import emit
from ahub.time import fmt_local

WORDS = {"merged": "слита", "dropped": "отброшена", "ready": "готова", "arbiter": "к арбитру", "failed": "упала",
         "stopped": "остановлена", "queued": "в очереди"}


def v1_db() -> Path:
    return Path.home() / ".local/share/agent-hub/hub.db"


def import_v1(project: config.ProjectConfig, db: Path | None = None) -> int:
    db = db or v1_db()
    if not db.exists():
        return 0
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        wt = project.worktrees
        rows = con.execute("SELECT id, project, stage, card_path, created_at, merged_sha, worktree, stage_reason"
                           " FROM task ORDER BY created_at").fetchall()
    finally:
        con.close()
    mine = [r for r in rows if r[1] == project.name or (not r[1] and wt and str(r[6]).startswith(wt))]
    lines = ["# Задачи старого хаба (v1)", "", "| Задача | Когда | Итог | Карточка | Слита |", "|---|---|---|---|---|"]
    for tid, _p, stage, card, created, merged, _w, reason in mine:
        lines.append(f"| {tid} | {fmt_local(int(created or 0))} | {WORDS.get(stage, stage)} | {card or '—'} |"
                     f" {str(merged or '')[:10] or '—'} |")
    base = archive.root(project)
    base.mkdir(parents=True, exist_ok=True)
    archive._exclude(project)
    (base / "v1-tasks.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(mine)


def cmd_import(args) -> int:
    projects, _ = config.load_projects()
    out = {}
    for p in projects:
        out[p.name] = import_v1(p)
    emit(args, out, "\n".join(f"{k}: {v} задач v1 → .agent-hub/v1-tasks.md" for k, v in out.items()) or "проектов нет")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("import-v1", help="история задач старого хаба — в архив проектов")
    p.set_defaults(func=cmd_import)
