"""Хранилище hub.db: задачи, сессии, события, вопросы, inbox, бюджеты."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

FINAL_STAGES = ("merged", "dropped")
# Этапы, которые может вернуть import_legacy / pipeline (§2 spec).
KNOWN_STAGES = (
    "queued", "preflight", "ready", "arbiter", "failed", "stopped",
    "merged", "dropped",
)

TASK_COLUMNS = (
    "id", "project", "card_path", "card_hash", "level", "branch",
    "worktree", "base_sha", "stage", "round", "executor",
    "reviewers_json", "stage_reason", "budget_go", "budget_usd",
    "created_at", "updated_at", "merged_sha", "rules_sha",
)


def default_path() -> Path:
    base = os.environ.get("AGENT_HUB_HOME") or str(Path.home() / ".local/share/agent-hub")
    return Path(base) / "hub.db"


class Store:
    """SQLite без долгоживущих соединений: каждый метод открывает/закрывает."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path))
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                "CREATE TABLE IF NOT EXISTS migration"
                "(name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)"
            )
            con.commit()
            applied = {r[0] for r in con.execute("SELECT name FROM migration")}
            for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if sql_file.name in applied:
                    continue
                con.executescript(sql_file.read_text(encoding="utf-8"))
                con.execute(
                    "INSERT INTO migration(name, applied_at) VALUES (?, ?)",
                    (sql_file.name, int(time.time() * 1000)),
                )
                con.commit()
        finally:
            con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(str(self.path))
        con.row_factory = sqlite3.Row
        return con

    # --- задачи ---

    def upsert_task(self, **fields) -> None:
        """Создать/обновить задачу. Обязательно поле id."""
        if "id" not in fields or not fields["id"]:
            raise ValueError("нужен id задачи")
        now = int(time.time() * 1000)
        fields.setdefault("created_at", now)
        fields["updated_at"] = fields.get("updated_at", now)
        cols = [c for c in TASK_COLUMNS if c in fields]
        placeholders = ", ".join("?" for _ in cols)
        # created_at пишется только при INSERT, обновление его не трогает.
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c not in ("id", "created_at"))
        con = self._connect()
        try:
            con.execute(
                f"INSERT INTO task({', '.join(cols)}) VALUES ({placeholders})"
                f" ON CONFLICT(id) DO UPDATE SET {updates}",
                [fields[c] for c in cols],
            )
            con.commit()
        finally:
            con.close()

    def get_task(self, task_id: str) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM task WHERE id=?", (task_id,)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def list_tasks(self, active_only: bool = True) -> list[dict]:
        con = self._connect()
        try:
            if active_only:
                rows = con.execute(
                    "SELECT * FROM task WHERE stage NOT IN ('merged','dropped')"
                    " ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = con.execute("SELECT * FROM task ORDER BY updated_at DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # --- сессии ---

    def link_session(
        self, external_id: str, tool: str, task_id: str,
        role: str, round: int, model: str,
    ) -> None:
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO session(external_id, tool, task_id, role, round, model, started_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(external_id) DO UPDATE SET"
                " tool=excluded.tool, task_id=excluded.task_id,"
                " role=excluded.role, round=excluded.round, model=excluded.model",
                (external_id, tool, task_id, role, round, model,
                 int(time.time() * 1000)),
            )
            con.commit()
        finally:
            con.close()

    def list_sessions(self, task_id: str | None = None) -> list[dict]:
        con = self._connect()
        try:
            if task_id is None:
                rows = con.execute("SELECT * FROM session ORDER BY started_at").fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM session WHERE task_id=? ORDER BY started_at",
                    (task_id,),
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # --- события ---

    def add_event(self, task_id: str, kind: str, payload: dict | None = None) -> int:
        con = self._connect()
        try:
            cur = con.execute(
                "INSERT INTO event(ts, task_id, kind, payload_json, seen_claude, sent_tg)"
                " VALUES (?, ?, ?, ?, 0, 0)",
                (int(time.time() * 1000), task_id, kind,
                 json.dumps(payload or {}, ensure_ascii=False)),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def events_since(self, event_id: int) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM event WHERE id > ? ORDER BY id", (event_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # --- импорт старого конвейера ---

    def import_legacy(self, worktrees_dir: str | Path) -> list[str]:
        """Прочитать <wt>/*/.agent/state.json (+ review_rN*.json).

        Возвращает ids заведённых/обновлённых задач.
        """
        wt_dir = Path(worktrees_dir)
        done: list[str] = []
        if not wt_dir.is_dir():
            return done
        for child in sorted(wt_dir.iterdir()):
            state_path = child / ".agent" / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            task_id = str(state.get("task") or child.name)
            status = str(state.get("status") or "failed")
            round_no = int(state.get("round") or 0)
            verdicts = state.get("verdicts") or []
            stage = _legacy_stage(status, round_no, verdicts, child)
            reviewers = state.get("reviewer_sessions") or []
            self.upsert_task(
                id=task_id,
                branch=f"agent/{task_id}",
                worktree=str(child),
                base_sha=str(state.get("base") or ""),
                stage=stage,
                round=round_no,
                reviewers_json=json.dumps(reviewers, ensure_ascii=False),
                stage_reason=",".join(str(v) for v in verdicts),
            )
            exec_sid = state.get("executor_session")
            if exec_sid and str(exec_sid) not in ("noop", "panel"):
                self.link_session(str(exec_sid), "opencode", task_id, "executor", round_no, "")
            for rsid in reviewers:
                if not rsid or str(rsid) in ("noop", "panel"):
                    continue
                self.link_session(str(rsid), "opencode", task_id, "reviewer", round_no, "")
            done.append(task_id)
        return done


def _legacy_stage(status: str, round_no: int, verdicts: list, worktree: Path) -> str:
    """Этап из status/round/verdicts (+ review-файлы при failed)."""
    if status in KNOWN_STAGES:
        if status == "failed" and round_no > 0:
            rev = _last_review_verdict(worktree, round_no)
            if rev == "changes":
                return f"review r{round_no}"
            if rev == "dispute":
                return "arbiter"
            # Review-файла может не быть (панель не дописала агрегат) —
            # тогда этап восстанавливаем по verdicts из state.json.
            vals = [str(v) for v in (verdicts or [])]
            if any(v == "changes" for v in vals):
                return f"review r{round_no}"
            if any(v == "dispute" for v in vals) or "invalid" in vals:
                return "arbiter"
        return status
    if status == "ready":
        return "ready"
    return "failed"


def _last_review_verdict(worktree: Path, round_no: int) -> str | None:
    """Первый verdict из review_rN*.json (приоритет dispute > changes > approve)."""
    try:
        cands = sorted((worktree / ".agent").glob(f"review_r{round_no}*.json"))
    except OSError:
        return None
    pats = re.compile(rf"^review_r{round_no}(?:_.*)?\.json$")
    found: set[str] = set()
    for path in cands:
        if not pats.match(path.name) or not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("verdict") in ("approve", "changes", "dispute"):
            found.add(str(data["verdict"]))
    for v in ("dispute", "changes", "approve"):
        if v in found:
            return v
    return None
