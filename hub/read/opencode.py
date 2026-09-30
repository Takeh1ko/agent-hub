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
    session_ids: list[str] | None = None,
) -> list[OcSession]:
    """Сессии opencode. Незнакомая схема → [] + warning, не исключение.

    Пакетно: детали всех сессий — фиксированным числом запросов
    (по одному на таблицу), а не запрос на сессию. БД — только mode=ro.
    """
    if session_ids is not None and len(session_ids) == 0:
        return []
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
            conds = ["time_updated >= ?"]
            args: list = [since_ms]
            if directory_prefix:
                conds.append("directory LIKE ?")
                args.append(directory_prefix + "%")
            if session_ids is not None:
                want = [str(x) for x in session_ids if str(x)]
                if not want:
                    return []
                conds.append(f"id IN ({','.join('?' for _ in want)})")
                args.extend(want)
            where = " AND ".join(conds)
            rows = con.execute(
                f"SELECT * FROM session WHERE {where} ORDER BY time_updated",
                tuple(args),
            ).fetchall()
        except sqlite3.Error as e:
            log.warning("opencode.db %s: незнакомая схема: %s", path, e)
            return []
        if not rows:
            return []
        now_ms = int(time.time() * 1000)
        try:
            return _batch(con, [dict(r) for r in rows], now_ms)
        except sqlite3.Error as e:
            log.warning("opencode.db: пакетное чтение не удалось: %s", e)
            # Фолбэк — по одной (медленно, но тот же результат).
            out: list[OcSession] = []
            for row in rows:
                try:
                    out.append(_one(con, dict(row), now_ms))
                except sqlite3.Error as e2:
                    log.warning("opencode.db: сессия пропущена: %s", e2)
            return out
    finally:
        con.close()


def _batch(con: sqlite3.Connection, srows: list[dict], now_ms: int) -> list[OcSession]:
    """Собрать OcSession фиксированным числом запросов (≤10 execute всего).

    Оконные функции (ROW_NUMBER) на живой базе дают ~0.8 с на 33 сессии:
    три прохода с сортировкой по разделам. Вместо них — два плоских
    SELECT без ORDER BY (сообщения + части одним куском) и разбор
    по сессиям в памяти: новейшие 20/5 берутся сортировкой маленьких
    групп в Python. Шаги step-finish считаются там же, без запроса.
    """
    ids = [str(s.get("id")) for s in srows if str(s.get("id") or "")]
    if not ids:
        return []
    ph = ",".join("?" for _ in ids)
    tup = tuple(ids)
    # Пульс: MAX(time_updated) по трём таблицам (дешёвые GROUP BY).
    pulse: dict[str, int] = {}
    for s in srows:
        sid = str(s.get("id"))
        try:
            pulse[sid] = int(s.get("time_updated") or s.get("time_created") or 0)
        except (TypeError, ValueError):
            pulse[sid] = 0
    for table in ("message", "part", "todo"):
        for sid2, mx in con.execute(
            f"SELECT session_id, MAX(time_updated) FROM {table}"
            f" WHERE session_id IN ({ph}) GROUP BY session_id",
            tup,
        ).fetchall():
            try:
                mx_i = int(mx) if mx else 0
            except (TypeError, ValueError):
                continue
            if mx_i > pulse.get(str(sid2), 0):
                pulse[str(sid2)] = mx_i
    # Сообщения и части — плоскими выборками, группировка в памяти.
    raw_msgs: dict[str, list[tuple[str, int]]] = {sid: [] for sid in pulse}
    for sid2, raw, t_upd in con.execute(
        f"SELECT session_id, data, time_updated FROM message"
        f" WHERE session_id IN ({ph})",
        tup,
    ).fetchall():
        k = str(sid2)
        if k not in raw_msgs:
            continue
        try:
            t_i = int(t_upd or 0)
        except (TypeError, ValueError):
            t_i = 0
        raw_msgs[k].append((str(raw or ""), t_i))
    msg_map: dict[str, list[tuple[str, int]]] = {}
    for k, lst in raw_msgs.items():
        lst.sort(key=lambda x: x[1], reverse=True)
        msg_map[k] = lst[:20]
    # Части: один проход, дальше шаги/tool/хвост из тех же списков.
    raw_parts: dict[str, list[tuple[str, int]]] = {sid: [] for sid in pulse}
    for sid2, raw, t_upd in con.execute(
        f"SELECT session_id, data, time_updated FROM part"
        f" WHERE session_id IN ({ph})",
        tup,
    ).fetchall():
        k = str(sid2)
        if k not in raw_parts:
            continue
        try:
            t_i = int(t_upd or 0)
        except (TypeError, ValueError):
            t_i = 0
        raw_parts[k].append((str(raw or ""), t_i))
    steps: dict[str, int] = {}
    tool_map: dict[str, list[tuple[str, int]]] = {}
    tail_map: dict[str, list[str]] = {}
    for k, lst in raw_parts.items():
        lst.sort(key=lambda x: x[1], reverse=True)
        tail_map[k] = [raw for raw, _ in lst[:5]]
        # Список уже отсортирован от новых к старым: первые 20 tool —
        # те же, что давало окно ROW_NUMBER; шаги — точный COUNT.
        tools: list[tuple[str, int]] = []
        n_steps = 0
        for raw, t_i in lst:
            try:
                p = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype == "step-finish":
                n_steps += 1
            elif ptype == "tool" and len(tools) < 20:
                tools.append((raw, t_i))
        steps[k] = n_steps
        tool_map[k] = tools
    out: list[OcSession] = []
    for s in srows:
        sid = str(s.get("id"))
        context = 0
        for raw in msg_map.get(sid, [])[:20]:
            data_s = raw[0] if isinstance(raw, tuple) else str(raw)
            try:
                m = json.loads(data_s)
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
        active_tool = ""
        active_age = 0
        activity = "думает"
        for raw, t_updated in tool_map.get(sid, [])[:20]:
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
                try:
                    active_age = max(0, (now_ms - int(start or now_ms)) // 1000)
                except (TypeError, ValueError):
                    active_age = 0
                activity = _tool_activity(tool, state)
                break
        if not active_tool:
            for raw in tail_map.get(sid, [])[:5]:
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
        model, provider = _model_info(s.get("model"))
        try:
            cost_f = float(s.get("cost") or 0.0)
        except (TypeError, ValueError):
            cost_f = 0.0
        out.append(OcSession(
            id=sid,
            directory=str(s.get("directory") or ""),
            title=str(s.get("title") or ""),
            model=model,
            provider=provider,
            started_ms=int(s.get("time_created") or 0),
            pulse_ms=int(pulse.get(sid, 0)),
            steps=int(steps.get(sid, 0)),
            tokens_in=int(s.get("tokens_input") or 0),
            tokens_out=int(s.get("tokens_output") or 0),
            cache_read=int(s.get("tokens_cache_read") or 0),
            cache_write=int(s.get("tokens_cache_write") or 0),
            cost=cost_f,
            context_tokens=context,
            active_tool=active_tool,
            active_tool_age_s=active_age,
            last_activity=activity[:60],
        ))
    return out


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
