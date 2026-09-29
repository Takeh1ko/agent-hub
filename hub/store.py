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

    # --- очередь H10: только выборки (без записи) ---

    def list_queued(self) -> list[dict]:
        """Задачи в stage=queued по created_at (порядок очереди)."""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM task WHERE stage='queued' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def list_queued_for_project(self, project_name: str, worktrees: str = "") -> list[dict]:
        """Queued своего проекта: project == имя или (пустой project + worktree внутри worktrees).

        Чужие проекты не возвращаются никогда. Без worktrees пустые project не берём.
        Нормализация — как у воркера (_belongs_to_project): имя/пути со strip,
        относительный worktree — от cwd.
        """
        want = str(project_name or "")
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM task WHERE stage='queued' ORDER BY created_at"
            ).fetchall()
            tasks = [dict(r) for r in rows]
        finally:
            con.close()
        wt_root = str(worktrees or "").strip().rstrip("/")
        out: list[dict] = []
        for t in tasks:
            tp = str(t.get("project") or "").strip()
            if tp:
                if tp == want:
                    out.append(t)
                continue
            if not wt_root:
                continue
            wt = str(t.get("worktree") or "").strip()
            if not wt:
                continue
            try:
                if not wt.startswith("/"):
                    wt = str(Path.cwd() / wt)
            except (OSError, ValueError):
                continue
            if wt == wt_root or wt.startswith(wt_root + "/"):
                out.append(t)
        return out

    def list_running_tasks(self) -> list[dict]:
        """Задачи в работе конвейера: preflight/exec/gate/review (для queue status)."""
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM task WHERE stage IN ('preflight', 'exec r1', 'exec r2',"
                " 'exec r3', 'exec r4', 'exec r5', 'gate r1', 'gate r2', 'gate r3',"
                " 'gate r4', 'gate r5', 'review r1', 'review r2', 'review r3',"
                " 'review r4', 'review r5')"
                " OR stage LIKE 'exec r%' OR stage LIKE 'gate r%' OR stage LIKE 'review r%'"
                " ORDER BY updated_at DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # --- черновики H15: текст владельца → карточка модели → запуск по кнопке ---

    def create_draft(self, project: str, text: str, source: str,
                     chat_id: int = 0, now_ms: int | None = None) -> int:
        """Завести черновик в статусе drafting. Возвращает id."""
        if source not in ("tg", "top", "cli"):
            raise ValueError(f"плохой source черновика: {source!r}")
        ts = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        con = self._connect()
        try:
            cur = con.execute(
                "INSERT INTO draft(ts, project, text, card_path, card_text,"
                " lint_errors, status, task_id, source, chat_id)"
                " VALUES (?, ?, ?, '', '', '', 'drafting', '', ?, ?)",
                (ts, str(project or ""), str(text or ""),
                 str(source), int(chat_id or 0)),
            )
            con.commit()
            return int(cur.lastrowid)
        finally:
            con.close()

    def get_draft(self, draft_id: int) -> dict | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM draft WHERE id=?",
                              (int(draft_id),)).fetchone()
            return dict(row) if row is not None else None
        finally:
            con.close()

    def list_drafts(self, status: str | None = None) -> list[dict]:
        con = self._connect()
        try:
            if status:
                rows = con.execute(
                    "SELECT * FROM draft WHERE status=? ORDER BY id",
                    (str(status),)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM draft ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def update_draft(self, draft_id: int, **fields) -> None:
        """Обновить поля черновика. Статус/source проверяются по контракту H15."""
        allowed = ("ts", "project", "text", "card_path", "card_text",
                   "lint_errors", "status", "task_id", "source", "chat_id")
        if "status" in fields and str(fields["status"]) not in (
                "drafting", "ready", "failed", "started", "cancelled"):
            raise ValueError(f"плохой status черновика: {fields['status']!r}")
        if "source" in fields and str(fields["source"]) not in ("tg", "top", "cli"):
            raise ValueError(f"плохой source черновика: {fields['source']!r}")
        cols = [c for c in allowed if c in fields]
        if not cols:
            return
        con = self._connect()
        try:
            con.execute(
                f"UPDATE draft SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?",
                [fields[c] for c in cols] + [int(draft_id)],
            )
            con.commit()
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

    def stage_marks(self) -> dict[str, tuple[int, str, str]]:
        """{task_id: (ts, причина, этап)} — последнее событие stage задачи.

        Этап в ответе — чтобы сверить с task.stage: не совпал — время этапа неизвестно.
        """
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT e.task_id, e.ts, e.payload_json FROM event e"
                " JOIN (SELECT task_id, MAX(id) AS mid FROM event"
                "       WHERE kind = 'stage' GROUP BY task_id) m ON e.id = m.mid",
            ).fetchall()
        finally:
            con.close()
        out: dict[str, tuple[int, str, str]] = {}
        for r in rows:
            try:
                payload = json.loads(r["payload_json"] or "{}") or {}
                reason = str(payload.get("reason") or "")
                stage = str(payload.get("stage") or "").strip()
            except (TypeError, ValueError, AttributeError):
                reason = stage = ""
            try:
                ts = int(r["ts"] or 0)
            except (TypeError, ValueError):
                ts = 0
            out[str(r["task_id"])] = (ts, reason, stage)
        return out

    def count_open_questions(self) -> int:
        """Сколько вопросов владельцу ждут ответа (question.status='open')."""
        con = self._connect()
        try:
            return int(con.execute(
                "SELECT count(*) FROM question WHERE status='open'").fetchone()[0])
        except sqlite3.Error:
            return 0
        finally:
            con.close()

    def recent_events(self, limit: int = 12) -> list[dict]:
        """Последние limit событий по возрастанию id (история для ленты top)."""
        try:
            limit = max(1, int(limit))
        except (TypeError, ValueError):
            limit = 12
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT * FROM (SELECT * FROM event ORDER BY id DESC LIMIT ?) ORDER BY id",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    # --- импорт старого конвейера ---

    def import_legacy(self, worktrees_dir: str | Path | None) -> list[str]:
        """Прочитать <wt>/*/.agent/state.json (+ review_rN*.json).

        Возвращает ids заведённых/обновлённых задач.
        Sweep снятых worktree — только когда каталог worktrees известен:
        задача из него пропала → ready → merged, остальные → dropped.
        queued/preflight не трогаем (каталог ещё может создаваться).
        Относительные пути сверяются от cwd.
        """
        done: list[str] = []
        wt_dir = Path(worktrees_dir) if worktrees_dir else None
        if wt_dir is not None and wt_dir.is_dir():
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
                fields = dict(
                    branch=f"agent/{task_id}",
                    worktree=str(child),
                    base_sha=str(state.get("base") or ""),
                    stage=stage,
                    round=round_no,
                    reviewers_json=json.dumps(reviewers, ensure_ascii=False),
                    stage_reason=",".join(str(v) for v in verdicts),
                )
                # Чтение (hub status, /status) не должно писать: обновляем строку, только
                # если legacy-поля реально изменились — иначе updated_at и пульс не трогаем.
                old = self.get_task(task_id)
                if old is None or any(str(old.get(k) or "") != str(v) for k, v in fields.items()):
                    self.upsert_task(id=task_id, **fields)
                exec_sid = state.get("executor_session")
                if exec_sid and str(exec_sid) not in ("noop", "panel"):
                    self.link_session(str(exec_sid), "opencode", task_id, "executor", round_no, "")
                for rsid in reviewers:
                    if not rsid or str(rsid) in ("noop", "panel"):
                        continue
                    self.link_session(str(rsid), "opencode", task_id, "reviewer", round_no, "")
                done.append(task_id)
        # Sweep: только когда каталог worktrees известен и задача из него
        # пропала. queued/preflight не трогаем (каталог ещё создаётся).
        # Без каталога (None/нет на диске) — только сканирование выше, без сноса.
        if wt_dir is not None and wt_dir.is_dir():
            try:
                root = wt_dir.expanduser()
                if not root.is_absolute():
                    root = Path.cwd() / root
            except OSError:
                return done
            try:
                tasks = self.list_tasks(active_only=False)
            except (OSError, ValueError, sqlite3.Error):
                return done
            for t in tasks:
                stage = str(t.get("stage") or "")
                if stage in FINAL_STAGES or stage in ("queued", "preflight"):
                    continue
                wt = str(t.get("worktree") or "")
                if not wt:
                    continue
                try:
                    p = Path(wt).expanduser()
                    if not p.is_absolute():
                        p = Path.cwd() / p
                except OSError:
                    continue
                try:
                    if p.exists():
                        continue
                except OSError:
                    continue
                try:
                    p.relative_to(root)
                except (ValueError, RuntimeError):
                    continue
                new_stage = "merged" if stage == "ready" else "dropped"
                try:
                    self.upsert_task(id=str(t.get("id") or ""), stage=new_stage)
                except (OSError, ValueError, sqlite3.Error):
                    continue
        return done


def _legacy_stage(status: str, round_no: int, verdicts: list, worktree: Path) -> str:
    """Этап из status/round/verdicts (+ review-файлы при failed)."""
    # Старый конвейер пишет status=failed с первой секунды как заглушку: пока нет
    # summary.md, задача в работе — этап по файлам текущего круга.
    agent = worktree / ".agent"
    n = max(int(round_no or 0), 1)
    vals = [str(v) for v in (verdicts or [])]
    if (status == "failed" and not (agent / "summary.md").exists() and len(vals) < n
            and not any(v in ("dispute", "invalid") for v in vals)):
        try:
            reviewing = any(agent.glob(f"reviewer_r{n}*.log")) or any(agent.glob(f"review_r{n}*.json"))
        except OSError:
            reviewing = False
        return f"review r{n}" if reviewing else f"exec r{n}"
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
