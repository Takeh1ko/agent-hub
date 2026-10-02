"""Read someone else's opencode database (SQLite) strictly read-only.

Usage, session state, and session search for the opencode provider.
Connections are ``mode=ro`` for the query duration only, no global state.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ahub import log
from ahub.i18n import t as _t
from ahub.providers.base import SessionState, Usage

# Id-list chunk for one IN (...): below SQLITE_LIMIT_VARIABLE_NUMBER.
_ID_CHUNK = 500

_REQUIRED_TABLES = ("session", "message", "part", "todo")
# Minimum columns actually read (the live schema is wider).
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
    """Path to someone else's opencode database: [paths].opencode_db → $XDG_DATA_HOME/… → ~/.local/share/…."""
    try:
        from ahub import config

        override = config.load_hub().opencode_db
        if override:
            return Path(override)
    except config.ConfigError:
        pass
    raw = os.environ.get("XDG_DATA_HOME")
    if raw:
        return Path(raw) / "opencode" / "opencode.db"
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
    # Read-only access to someone else's file; timeout — don't hang on their lock.
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
    """What's missing from the schema (empty — everything needed is there)."""
    out: list[str] = []
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in _REQUIRED_TABLES:
        if t not in tables:
            out.append(_t("odb.no_table", name=t))
    for t, want in _REQ_COLS.items():
        if t not in tables:
            continue
        cols = {r[1] for r in con.execute(f"PRAGMA table_info('{t}')").fetchall()}
        for c in want:
            if c not in cols:
                out.append(_t("odb.no_column", table=t, col=c))
    return out


def check_schema(db_path: str | Path | None = None) -> SchemaStatus:
    """Whether the file and the needed tables/columns exist. Never raises."""
    path = _resolve(db_path)
    if not Path(path).exists():
        return SchemaStatus(ok=False, problems=[_t("odb.no_db", path=path)])
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db cannot open %s: %s", path, e)
            return SchemaStatus(ok=False, problems=[_t("odb.no_db", path=path)])
        probs = _problems(con)
        if probs:
            _warn("opencode.db %s: unknown schema: %s", path, "; ".join(probs))
            return SchemaStatus(ok=False, problems=probs)
        return SchemaStatus(ok=True, problems=[])
    except sqlite3.Error as e:
        _warn("opencode.db %s: schema unreadable: %s", path, e)
        return SchemaStatus(ok=False, problems=[_t("odb.bad_schema", err=e)])
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
    # session.model — JSON {"id": ..., "providerID": ...}; broken — treat as foreign.
    try:
        info = json.loads(str(model_raw or "{}"))
    except json.JSONDecodeError:
        return ""
    if not isinstance(info, dict):
        return ""
    return str(info.get("providerID") or "")


def _split_cost(cost: float, provider: str) -> tuple[float, float]:
    # go — subscription (money at "list price"), everything else — real money.
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
    """input + cache.read of the newest live assistant (total > 0, no error)."""
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
            # New format: require total > 0.
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
            # Old format without total: zero contribution — a truncated retry.
            continue
        return contrib
    return 0


def session_usage(session_id: str, db_path: str | Path | None = None) -> Usage | None:
    """Session usage from session-row aggregates + context from messages."""
    if not session_id:
        return None
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db cannot open %s: %s", path, e)
            return None
        try:
            if _problems(con):
                _warn("opencode.db %s: unknown schema", path)
                return None
            row = con.execute(
                "SELECT cost, model, tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _warn("opencode.db %s: read session: %s", path, e)
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
            _warn("opencode.db %s: read messages: %s", path, e)
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
    """Batch usage (IN of 500 ids), no context; unknown ids are absent from the dict."""
    want = [str(x) for x in session_ids if str(x)]
    if not want:
        return {}
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db cannot open %s: %s", path, e)
            return {}
        try:
            if _problems(con):
                _warn("opencode.db %s: unknown schema", path)
                return {}
        except sqlite3.Error as e:
            _warn("opencode.db %s: schema unreadable: %s", path, e)
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
                _warn("opencode.db %s: batch read: %s", path, e)
                return {}
            for r in rows:
                try:
                    out[str(r["id"])] = _usage_from_row(r, None)
                except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as e:
                    _warn("opencode.db: session skipped: %s", e)
        return out
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return {}
    finally:
        _close(con)


def session_state(session_id: str, db_path: str | Path | None = None) -> SessionState | None:
    """Pulse, active tool, finished flag, and session usage."""
    if not session_id:
        return None
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db cannot open %s: %s", path, e)
            return None
        try:
            if _problems(con):
                _warn("opencode.db %s: unknown schema", path)
                return None
            srow = con.execute(
                "SELECT cost, model, tokens_input, tokens_output, tokens_reasoning,"
                " tokens_cache_read, tokens_cache_write,"
                " time_created, time_updated FROM session WHERE id=?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error as e:
            _warn("opencode.db %s: read session: %s", path, e)
            return None
        if srow is None:
            return None
        try:
            # Pulse — max across all tables of this session.
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
            _warn("opencode.db %s: read details: %s", path, e)
            return None
        # Active tool — the newest tool with running status.
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
        # Finished — the last assistant has time.completed and finish.
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
    """Newest session without parent_id in the directory within the window (5 s tolerance)."""
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db cannot open %s: %s", path, e)
            return None
        try:
            if _problems(con):
                _warn("opencode.db %s: unknown schema", path)
                return None
            row = con.execute(
                "SELECT id FROM session WHERE directory=? AND time_created>=?"
                " AND (parent_id IS NULL OR parent_id='')"
                " ORDER BY time_created DESC LIMIT 1",
                (directory, started_after_ms - 5000),
            ).fetchone()
        except sqlite3.Error as e:
            _warn("opencode.db %s: find session: %s", path, e)
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
    """Totals over sessions with time_updated in [since, until): go/usd separately."""
    zeros = Usage(tokens_in=0, tokens_out=0, tokens_reasoning=0,
                  cache_read=0, cache_write=0, cost_go=0.0, cost_usd=0.0)
    path = _resolve(db_path)
    con: sqlite3.Connection | None = None
    try:
        try:
            con = _open(path)
        except sqlite3.Error as e:
            _warn("opencode.db cannot open %s: %s", path, e)
            return zeros
        try:
            if _problems(con):
                _warn("opencode.db %s: unknown schema", path)
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
            _warn("opencode.db %s: read totals: %s", path, e)
            return zeros
        acc = zeros
        for r in rows:
            try:
                acc = acc.add(_usage_from_row(r, None))
            except (TypeError, ValueError, json.JSONDecodeError) as e:
                _warn("opencode.db: totals row skipped: %s", e)
        acc.context = None
        return acc
    except sqlite3.Error as e:
        _warn("opencode.db %s: %s", path, e)
        return zeros
    finally:
        _close(con)
