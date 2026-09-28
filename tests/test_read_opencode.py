"""Фейковая opencode.db: пульс, active_tool, go/usd, контекст, чужая схема."""

from __future__ import annotations

import json
import logging
import sqlite3

from hub.read.opencode import sessions

SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
  title TEXT NOT NULL, model TEXT, cost REAL DEFAULT 0 NOT NULL,
  tokens_input INTEGER DEFAULT 0 NOT NULL, tokens_output INTEGER DEFAULT 0 NOT NULL,
  tokens_cache_read INTEGER DEFAULT 0 NOT NULL, tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
  priority TEXT NOT NULL, position INTEGER NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
"""


def _db(path, now: int):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    # Активная go-сессия: свежий пульс, running bash, последний assistant с кэшем.
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_go", "p", "/wt/T01", "задача", json.dumps({"id": "muse", "providerID": "opencode-go"}),
         0.5, 100, 50, 1000, 200, now - 60_000, now - 30_000))
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m1", "ses_go", now - 60_000, now - 50_000, json.dumps({
            "role": "assistant", "tokens": {"input": 1000, "cache": {"read": 4000}}})))
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p1", "m1", "ses_go", now - 40_000, now - 30_000, json.dumps({
            "type": "tool", "tool": "bash",
            "state": {"status": "running",
                      "input": {"command": "pytest -q tests/"},
                      "time": {"start": now - 25_000}}})))
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p2", "m1", "ses_go", now - 35_000, now - 20_000, json.dumps({"type": "step-finish"})))
    # Старая usd-сессия без активного tool.
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_usd", "p", "/wt/T02", "вторая", json.dumps({"id": "m/mimo", "providerID": "openrouter"}),
         0.25, 10, 5, 0, 0, now - 9_000_000, now - 8_000_000))
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m2", "ses_usd", now - 9_000_000, now - 8_500_000, json.dumps({
            "role": "assistant", "tokens": {"input": 500, "cache": {"read": 100}}})))
    con.commit()
    con.close()
    return path


NOW = 1_789_000_000_000


def test_pulse_active_tool_context(tmp_path):
    db = _db(tmp_path / "opencode.db", NOW)
    got = {s.id: s for s in sessions(db, 0)}
    go = got["ses_go"]
    assert go.pulse_ms == NOW - 20_000  # max по part (шаг новее сообщения)
    assert go.active_tool == "bash"
    assert "pytest" in go.last_activity
    assert len(go.last_activity) <= 60
    assert go.context_tokens == 1000 + 4000  # input + cache.read последнего assistant
    assert go.provider == "opencode-go" and go.model == "muse"
    assert go.steps == 1
    old = got["ses_usd"]
    assert old.active_tool == ""
    assert old.context_tokens == 600
    assert old.last_activity == "думает"


def test_since_and_prefix_filter(tmp_path):
    db = _db(tmp_path / "opencode.db", NOW)
    assert {s.id for s in sessions(db, NOW - 60_000)} == {"ses_go"}
    assert {s.id for s in sessions(db, 0, "/wt/T02")} == {"ses_usd"}
    assert sessions(db, 0, "/wt/нет") == []


def test_go_usd_split(tmp_path):
    db = _db(tmp_path / "opencode.db", NOW)
    got = {s.id: s for s in sessions(db, 0)}
    go = sum(s.cost for s in got.values() if s.provider == "opencode-go")
    usd = sum(s.cost for s in got.values() if s.provider != "opencode-go")
    assert (go, usd) == (0.5, 0.25)


def test_unknown_schema_returns_empty(tmp_path, caplog):
    db = tmp_path / "чужой.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE совсем_другое (id INTEGER)")
    con.commit()
    con.close()
    with caplog.at_level(logging.WARNING):
        assert sessions(db, 0) == []
    assert caplog.text.strip() != ""


def test_unknown_columns_returns_empty(tmp_path, caplog):
    """Таблицы свои, а колонки чужие (нет time_updated) → [] + warning."""
    db = tmp_path / "полусвой.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT NOT NULL,"
                " title TEXT NOT NULL, model TEXT, cost REAL, time_created INTEGER NOT NULL)")
    con.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,"
                " time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
    con.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL,"
                " session_id TEXT NOT NULL, time_created INTEGER NOT NULL,"
                " time_updated INTEGER NOT NULL, data TEXT NOT NULL)")
    con.execute("CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL,"
                " status TEXT NOT NULL, priority TEXT NOT NULL, position INTEGER NOT NULL,"
                " time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL)")
    con.execute("INSERT INTO session VALUES (?,?,?,?,?,?)",
                ("s", "/wt/T", "t", "{}", 0.0, NOW))
    con.commit()
    con.close()
    with caplog.at_level(logging.WARNING):
        assert sessions(db, 0) == []
    assert caplog.text.strip() != ""


def test_context_skips_error_and_zero_tokens(tmp_path):
    """Последний assistant оборван (error, нули) → контекст из предыдущего живого."""
    con = sqlite3.connect(str(tmp_path / "opencode.db"))
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s", "p", "/wt/T", "t", json.dumps({"id": "x", "providerID": "openrouter"}),
         0.0, 0, 0, 0, 0, NOW - 100_000, NOW))
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m_old", "s", NOW - 90_000, NOW - 90_000, json.dumps({
            "role": "assistant", "tokens": {"input": 2000, "cache": {"read": 8000}}})))
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m_zero", "s", NOW - 50_000, NOW - 50_000, json.dumps({
            "role": "assistant", "tokens": {"input": 0, "cache": {"read": 0}}})))
    con.execute(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        ("m_new", "s", NOW - 10_000, NOW - 10_000, json.dumps({
            "role": "assistant",
            "error": {"name": "MessageAbortedError"},
            "tokens": {"input": 0, "cache": {"read": 0}}})))
    con.commit()
    con.close()
    (got,) = sessions(tmp_path / "opencode.db", 0)
    assert got.context_tokens == 2000 + 8000


def test_last_activity_edit_format(tmp_path):
    con = sqlite3.connect(str(tmp_path / "opencode.db"))
    con.executescript(SCHEMA)
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("s", "p", "/wt/T", "t", json.dumps({"id": "x", "providerID": "openrouter"}),
         0.0, 0, 0, 0, 0, NOW, NOW))
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p", "m", "s", NOW, NOW, json.dumps({
            "type": "tool", "tool": "edit",
            "state": {"status": "running", "input": {"filePath": "worker/x.py"}}})))
    con.commit()
    con.close()
    (got,) = sessions(tmp_path / "opencode.db", 0)
    assert got.last_activity == "edit worker/x.py"
