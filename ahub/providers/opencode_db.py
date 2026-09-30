"""Чтение чужой базы opencode (SQLite) строго только на чтение.

Учёт, состояние сессии и поиск сессии для провайдера opencode.
Соединение — только ``mode=ro`` на время запроса, без глобального состояния.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ahub import log
from ahub.providers.base import SessionState, Usage

# Кусок списка id для одного IN (...): ниже SQLITE_LIMIT_VARIABLE_NUMBER.
_ID_CHUNK = 500

_REQUIRED_TABLES = ("session", "message", "part", "todo")
# Минимум колонок, которые реально читаем (живая схема шире).
_REQ_COLS: dict[str, tuple[str, ...]] = {
    "session": ("id", "parent_id", "directory", "model", "cost",
                "tokens_input", "tokens_output", "tokens_reasoning",
                "tokens_cache_read", "tokens_cache_write",
                "time_created", "time_updated"),
    "message": ("session_id", "data", "time_updated"),
    "part": ("session_id", "data", "time_created", "time_updated"),
    "todo": ("session_id", "time_updated"),
}


def default_db() -> Path:
    """Путь к чужой базе opencode (HOME читается при вызове)."""
    return Path.home() / ".local/share/opencode/opencode.db"


@dataclass
class SchemaStatus:
    ok: bool
    problems: list[str] = field(default_factory=list)


def _resolve(db_path: str | Path | None) -> str:
    return str(default_db()) if db_path is None else str(db_path)


def _warn(msg: str, *args: object) -> None:
    log.get("opencode").warning(msg, *args)


def _open(path: str) -> sqlite3.Connection:
    # Только чтение чужого файла; timeout — не висеть на чужом локе.
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _close(con: sqlite3.Connection | None) -> None:
    if con is not None:
        try:
            con.close()
        except sqlite3.Error:
            pass


def _problems(con: sqlite3.Connection) -> list[str]:
    """Чего не хватает в схеме (пусто — всё нужное есть)."""
    out: list[str] = []
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in _REQUIRED_TABLES:
        if t not in tables:
            out.append(f"нет таблицы {t}")
    for t, want in _REQ_COLS.items():
        if t not in tables:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()}
        for c in want:
            if c not in cols:
                out.append(f"нет колонки {t}.{c}")
    return out


def check_schema(db_path: str | Path | None = None) -> SchemaStatus:
    """Есть ли файл и нужные таблицы/колонки. Не бросает."""
    path = _resolve(db_path)
    if not Path(path).exists():
        return SchemaStatus(ok=False, problems=[f"нет базы {path}"])
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db не открылась %s: %s", path, e)
            return SchemaStatus(ok=False, problems=[f"нет базы {path}"])
        probs = _problems(con)
        if probs:
            _warn("opencode.db %s: незнакомая схема: %s", path, "; ".join(probs))
            return SchemaStatus(ok=False, problems=probs)
        return SchemaStatus(ok=True, problems=[])
    except sqlite3.Error as e:
        _warn("opencode.db %s: схема не читается: %s", path, e)
        return SchemaStatus(ok=False, problems=[f"схема не читается: {e}"])
    finally:
        _close(con)


def _to_int(v: object, default: int = 0) -> int:
    try:
        return int(v or 0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _to_float(v: object, default: float = 0.0) -> float:
    try:
        return float(v or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _provider_id(model_raw: object) -> str:
    # session.model — JSON {"id": ..., "providerID": ...}; битый — как чужой.
    try:
        info = json.loads(str(model_raw or "{}"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(info, dict):
        return ""
    return str(info.get("providerID") or "")


def _split_cost(cost: float, provider: str) -> tuple[float, float]:
    # go — подписка (деньги «по прайсу»), остальное — реальные деньги.
    if provider == "opencode-go":
        return cost, 0.0
    return 0.0, cost


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)] if items else []


def _usage_from_row(row: sqlite3.Row, context: int | None) -> Usage:
    cost = _to_float(row["cost"])
    go, usd = _split_cost(cost, _provider_id(row["model"]))
    return Usage(
        tokens_in=_to_int(row["tokens_input"]),
        tokens_out=_to_int(row["tokens_output"]),
        tokens_reasoning=_to_int(row["tokens_reasoning"]),
        cache_read=_to_int(row["tokens_cache_read"]),
        cache_write=_to_int(row["tokens_cache_write"]),
        cost_go=go,
        cost_usd=usd,
        context=context,
    )


def _live_context(msgs: list[sqlite3.Row]) -> int:
    """input + cache.read новейшего живого assistant (total > 0, без error)."""
    for (raw,) in msgs:
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(m, dict):
            continue
        if m.get("role") != "assistant":
            continue
        if m.get("error"):
            continue
        toks = m.get("tokens") or {}
        if not isinstance(toks, dict):
            continue
        total_raw = toks.get("total")
        if total_raw is not None:
            # Новый формат: требуем total > 0.
            try:
                if int(total_raw or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
        try:
            cache = toks.get("cache") or {}
            if not isinstance(cache, dict):
                cache = {}
            contrib = int(toks.get("input") or 0) + int(cache.get("read") or 0)
        except (TypeError, ValueError):
            continue
        if total_raw is None and contrib <= 0:
            # Старый формат без total: нулевой вклад — оборванный повтор.
            continue
        return contrib
    return 0


def session_usage(session_id: str, db_path: str | Path | None = None) -> Usage | None:
    """Учёт сессии из агрегатов строки session + контекст из сообщений."""
    if not session_id:
        return None
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db не открылась %s: %s", path, e)
            return None
        try:
            if _problems(con):
                _warn("opencode.db %s: незнакомая схема", path)
                return None
            row = con.execute(
                "SELECT cost, model, tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _warn("opencode.db %s: чтение сессии: %s", path, e)
            return None
        if row is None:
            return None
        try:
            msgs = con.execute(
                "SELECT data FROM message WHERE session_id=?"
                " ORDER BY time_updated DESC LIMIT 20",
                (session_id,),
            ).fetchall()
        except sqlite3.Error as e:
            _warn("opencode.db %s: чтение сообщений: %s", path, e)
            return None
        return _usage_from_row(row, _live_context(msgs))
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return None
    finally:
        _close(con)


def sessions_usage(
    session_ids: list[str], db_path: str | Path | None = None,
) -> dict[str, Usage]:
    """Учёт пачкой (IN по 500 id), без контекста; чужих id нет в словаре."""
    want = [str(x) for x in session_ids if str(x)]
    if not want:
        return {}
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db не открылась %s: %s", path, e)
            return {}
        try:
            if _problems(con):
                _warn("opencode.db %s: незнакомая схема", path)
                return {}
        except sqlite3.Error as e:
            _warn("opencode.db %s: схема не читается: %s", path, e)
            return {}
        out: dict[str, Usage] = {}
        for part_ids in _chunks(want, _ID_CHUNK):
            ph = ",".join("?" for _ in part_ids)
            try:
                rows = con.execute(
                    "SELECT id, cost, model, tokens_input, tokens_output,"
                    " tokens_reasoning, tokens_cache_read, tokens_cache_write"
                    f" FROM session WHERE id IN ({ph})",
                    tuple(part_ids),
                ).fetchall()
            except sqlite3.Error as e:
                _warn("opencode.db %s: пакетное чтение: %s", path, e)
                return {}
            for r in rows:
                try:
                    out[str(r["id"])] = _usage_from_row(r, None)
                except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as e:
                    _warn("opencode.db: сессия пропущена: %s", e)
        return out
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return {}
    finally:
        _close(con)


def session_state(session_id: str, db_path: str | Path | None = None) -> SessionState | None:
    """Пульс, активный инструмент, finished и учёт сессии."""
    if not session_id:
        return None
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db не открылась %s: %s", path, e)
            return None
        try:
            if _problems(con):
                _warn("opencode.db %s: незнакомая схема", path)
                return None
            srow = con.execute(
                "SELECT cost, model, tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write,"
                " time_created, time_updated FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _warn("opencode.db %s: чтение сессии: %s", path, e)
            return None
        if srow is None:
            return None
        try:
            # Пульс — максимум по всем таблицам этой сессии.
            last = _to_int(srow["time_updated"]) or _to_int(srow["time_created"])
            for table in ("message", "part", "todo"):
                r = con.execute(
                    f"SELECT MAX(time_updated) FROM {table} WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if r and r[0]:
                    last = max(last, _to_int(r[0]))
            msgs = con.execute(
                "SELECT data FROM message WHERE session_id=?"
                " ORDER BY time_updated DESC LIMIT 50",
                (session_id,),
            ).fetchall()
            parts = con.execute(
                "SELECT data, time_created, time_updated FROM part"
                " WHERE session_id=? ORDER BY time_updated DESC LIMIT 50",
                (session_id,),
            ).fetchall()
        except sqlite3.Error as e:
            _warn("opencode.db %s: чтение деталей: %s", path, e)
            return None
        # Активный инструмент — свежайший tool со статусом running.
        active_tool = ""
        tool_started: int | None = None
        for prow in parts:
            try:
                p = json.loads(prow["data"])
            except json.JSONDecodeError:
                continue
            if not isinstance(p, dict) or p.get("type") != "tool":
                continue
            state = p.get("state") or {}
            if not isinstance(state, dict) or state.get("status") != "running":
                continue
            active_tool = str(p.get("tool") or "")
            start: object = None
            t = state.get("time")
            if isinstance(t, dict):
                start = t.get("start")
            try:
                tool_started = int(start) if start else _to_int(prow["time_created"])
            except (TypeError, ValueError):
                tool_started = _to_int(prow["time_created"])
            break
        # Finished — у последнего assistant есть time.completed и finish.
        finished = False
        for (raw,) in msgs:
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            t = m.get("time") or {}
            completed = t.get("completed") if isinstance(t, dict) else None
            finished = completed is not None and m.get("finish") is not None
            break
        return SessionState(
            last_activity_ms=last,
            active_tool=active_tool,
            tool_started_ms=tool_started,
            finished=finished,
            usage=_usage_from_row(srow, _live_context(msgs)),
        )
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return None
    finally:
        _close(con)


def find_session(
    directory: str, started_after_ms: int, db_path: str | Path | None = None,
) -> str | None:
    """Новейшая сессия без parent_id в каталоге не старше окна (допуск 5 с)."""
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db не открылась %s: %s", path, e)
            return None
        try:
            if _problems(con):
                _warn("opencode.db %s: незнакомая схема", path)
                return None
            row = con.execute(
                "SELECT id FROM session WHERE directory=? AND time_created>=?"
                " AND (parent_id IS NULL OR parent_id='')"
                " ORDER BY time_created DESC LIMIT 1",
                (directory, started_after_ms - 5000),
            ).fetchone()
        except sqlite3.Error as e:
            _warn("opencode.db %s: поиск сессии: %s", path, e)
            return None
        return str(row[0]) if row and row[0] else None
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return None
    finally:
        _close(con)


def totals(
    since_ms: int, db_path: str | Path | None = None, until_ms: int | None = None,
) -> Usage:
    """Суммы по сессиям с time_updated в [since, until): go/usd раздельно."""
    zeros = Usage(tokens_in=0, tokens_out=0, tokens_reasoning=0,
                  cache_read=0, cache_write=0, cost_go=0.0, cost_usd=0.0)
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db не открылась %s: %s", path, e)
            return zeros
        try:
            if _problems(con):
                _warn("opencode.db %s: незнакомая схема", path)
                return zeros
            if until_ms is None:
                rows = con.execute(
                    "SELECT cost, model, tokens_input, tokens_output,"
                    " tokens_reasoning, tokens_cache_read, tokens_cache_write"
                    " FROM session WHERE time_updated>=?",
                    (since_ms,),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT cost, model, tokens_input, tokens_output,"
                    " tokens_reasoning, tokens_cache_read, tokens_cache_write"
                    " FROM session WHERE time_updated>=? AND time_updated<?",
                    (since_ms, until_ms),
                ).fetchall()
        except sqlite3.Error as e:
            _warn("opencode.db %s: чтение итогов: %s", path, e)
            return zeros
        acc = zeros
        for r in rows:
            try:
                acc = acc.add(_usage_from_row(r, None))
            except (TypeError, ValueError, json.JSONDecodeError) as e:
                _warn("opencode.db: строка итогов пропущена: %s", e)
        acc.context = None
        return acc
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return zeros
    finally:
        _close(con)
