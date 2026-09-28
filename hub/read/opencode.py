"""Чтение чужой opencode.db — строго mode=ro, соединение только на запрос."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED_TABLES = ("session", "message", "part", "todo")
# Минимум колонок session: живая БД меняется быстрее тестов.
REQUIRED_SESSION_COLS = ("time_updated", "time_created", "directory", "cost", "model")


@dataclass
class OcSession:
    id: str
    directory: str
    title: str
    model: str
    provider: str
    started_ms: int
    pulse_ms: int
    steps: int
    tokens_in: int
    tokens_out: int
    cache_read: int
    cache_write: int
    cost: float
    context_tokens: int
    active_tool: str
    active_tool_age_s: int
    last_activity: str


def _model_info(raw: str | None) -> tuple[str, str]:
    try:
        info = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return "?", ""
    mid = str(info.get("id") or "?").split("/")[-1]
    return mid, str(info.get("providerID") or "")


def _tool_activity(tool: str, state: dict) -> str:
    inp = state.get("input") or {}
    if tool == "bash":
        cmd = str(inp.get("command") or inp.get("cmd") or "")
        return f"bash: {cmd}"[:60] or "bash"
    for key in ("filePath", "path", "file", "filename"):
        if inp.get(key):
            return f"{tool} {inp[key]}"[:60]
    title = str((state.get("title") or inp.get("title") or ""))
    if title:
        return f"{tool}: {title}"[:60]
    return tool[:60]


def sessions(
    db_path: str | Path, since_ms: int, directory_prefix: str | None = None,
) -> list[OcSession]:
    """Сессии opencode. Незнакомая схема → [] + warning, не исключение."""
    path = str(db_path)
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        log.warning("opencode.db не открылась %s: %s", path, e)
        return []
    try:
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as e:
            log.warning("opencode.db %s: схема не читается: %s", path, e)
            return []
        if any(t not in tables for t in REQUIRED_TABLES):
            log.warning("opencode.db %s: незнакомая схема (нет %s)", path, REQUIRED_TABLES)
            return []
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info('session')").fetchall()}
        except sqlite3.Error as e:
            log.warning("opencode.db %s: схема не читается: %s", path, e)
            return []
        if any(c not in cols for c in REQUIRED_SESSION_COLS):
            log.warning("opencode.db %s: незнакомая схема (нет колонок %s)",
                        path, REQUIRED_SESSION_COLS)
            return []
        try:
            if directory_prefix:
                rows = con.execute(
                    "SELECT * FROM session WHERE time_updated >= ? AND directory LIKE ?"
                    " ORDER BY time_updated",
                    (since_ms, directory_prefix + "%"),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM session WHERE time_updated >= ? ORDER BY time_updated",
                    (since_ms,),
                ).fetchall()
        except sqlite3.Error as e:
            log.warning("opencode.db %s: незнакомая схема: %s", path, e)
            return []
        now_ms = int(time.time() * 1000)
        out: list[OcSession] = []
        for row in rows:
            try:
                out.append(_one(con, dict(row), now_ms))
            except sqlite3.Error as e:
                log.warning("opencode.db: сессия пропущена: %s", e)
        return out
    finally:
        con.close()


def _one(con: sqlite3.Connection, s: dict, now_ms: int) -> OcSession:
    sid = str(s.get("id"))
    pulse = int(s.get("time_updated") or s.get("time_created") or 0)
    for table in ("message", "part", "todo"):
        try:
            r = con.execute(
                f"SELECT MAX(time_updated) FROM {table} WHERE session_id=?", (sid,),
            ).fetchone()
        except sqlite3.Error:
            continue
        if r and r[0]:
            pulse = max(pulse, int(r[0]))
    steps = 0
    try:
        steps = int(con.execute(
            "SELECT COUNT(*) FROM part WHERE session_id=? AND"
            " json_extract(data, '$.type')='step-finish'", (sid,),
        ).fetchone()[0])
    except sqlite3.Error:
        steps = 0
    # Контекст: input + cache.read последнего «живого» assistant-сообщения
    # (без error и с ненулевым вкладом — оборванные повторы дают нули).
    context = 0
    try:
        msgs = con.execute(
            "SELECT data FROM message WHERE session_id=? ORDER BY time_updated DESC LIMIT 20",
            (sid,),
        ).fetchall()
        for (raw,) in msgs:
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if m.get("role") != "assistant":
                continue
            if m.get("error"):
                continue
            toks = m.get("tokens") or {}
            cache = toks.get("cache") or {}
            try:
                contrib = int(toks.get("input") or 0) + int(cache.get("read") or 0)
            except (TypeError, ValueError):
                continue
            if contrib <= 0:
                continue
            context = contrib
            break
    except sqlite3.Error:
        context = 0
    # Активный tool: последний part type=tool со статусом pending/running.
    active_tool = ""
    active_age = 0
    activity = "думает"
    try:
        parts = con.execute(
            "SELECT data, time_updated FROM part WHERE session_id=?"
            " AND json_extract(data, '$.type')='tool'"
            " ORDER BY time_updated DESC LIMIT 20",
            (sid,),
        ).fetchall()
        for raw, t_updated in parts:
            try:
                p = json.loads(raw)
            except json.JSONDecodeError:
                continue
            status = (p.get("state") or {}).get("status")
            if status in ("pending", "running"):
                tool = str(p.get("tool") or "")
                active_tool = tool
                state = p.get("state") or {}
                start = ((state.get("time") or {}).get("start")
                         if isinstance(state.get("time"), dict) else None) or t_updated
                active_age = max(0, (now_ms - int(start or now_ms)) // 1000)
                activity = _tool_activity(tool, state)
                break
        if not active_tool:
            # Без активного tool: последний текст или «думает».
            tail = con.execute(
                "SELECT data FROM part WHERE session_id=? ORDER BY time_updated DESC LIMIT 5",
                (sid,),
            ).fetchall()
            for (raw,) in tail:
                try:
                    p = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if p.get("type") == "text" and p.get("text"):
                    activity = str(p["text"])[:60]
                    break
                if p.get("type") == "reasoning":
                    activity = "думает"
                    break
    except sqlite3.Error:
        pass
    model, provider = _model_info(s.get("model"))
    return OcSession(
        id=sid,
        directory=str(s.get("directory") or ""),
        title=str(s.get("title") or ""),
        model=model,
        provider=provider,
        started_ms=int(s.get("time_created") or 0),
        pulse_ms=pulse,
        steps=steps,
        tokens_in=int(s.get("tokens_input") or 0),
        tokens_out=int(s.get("tokens_output") or 0),
        cache_read=int(s.get("tokens_cache_read") or 0),
        cache_write=int(s.get("tokens_cache_write") or 0),
        cost=float(s.get("cost") or 0.0),
        context_tokens=context,
        active_tool=active_tool,
        active_tool_age_s=active_age,
        last_activity=activity[:60],
    )
