"""Fake opencode.db: go/usd accounting, context, pulse, session lookup, totals, foreign schema."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from ahub.providers import opencode_db as odb

NOW = 1_789_000_000_000

SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, workspace_id TEXT,
  parent_id TEXT, slug TEXT, directory TEXT, path TEXT, title TEXT, version TEXT,
  cost REAL DEFAULT 0 NOT NULL,
  tokens_input INTEGER DEFAULT 0 NOT NULL, tokens_output INTEGER DEFAULT 0 NOT NULL,
  tokens_reasoning INTEGER DEFAULT 0 NOT NULL,
  tokens_cache_read INTEGER DEFAULT 0 NOT NULL, tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
  agent TEXT, model TEXT,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL,
  time_compacting INTEGER, time_archived INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
  priority TEXT NOT NULL, position INTEGER NOT NULL,
  time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
"""


def _mkdb(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    return con


def _ses(con, sid, *, directory="/wt/T", provider="opencode-go", cost=0.0,
         tin=0, tout=0, tr=0, cr=0, cw=0, t_created=NOW, t_updated=NOW,
         parent=None, model_id="muse-spark"):
    con.execute(
        "INSERT INTO session (id, project_id, workspace_id, parent_id, slug, directory,"
        " path, title, version, cost, tokens_input, tokens_output, tokens_reasoning,"
        " tokens_cache_read, tokens_cache_write, agent, model,"
        " time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, "p", "w", parent, "s", directory, directory, "t", "v",
         cost, tin, tout, tr, cr, cw, "a",
         json.dumps({"id": model_id, "providerID": provider, "variant": "xhigh"}),
         t_created, t_updated))


def _msg(con, mid, sid, data: dict, t=NOW):
    con.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                (mid, sid, t, t, json.dumps(data)))


def _part(con, pid, mid, sid, data: dict, t=NOW, t_created=None):
    con.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                (pid, mid, sid, t_created if t_created is not None else t,
                 t, json.dumps(data)))


def _todo(con, sid, t=NOW):
    con.execute("INSERT INTO todo VALUES (?,?,?,?,?,?,?)",
                (sid, "c", "pending", "medium", 0, t, t))


def _assist(*, total, inp=0, read=0, out=0, err=None, completed=None, finish=None,
            model="m"):
    d: dict = {"role": "assistant", "modelID": model, "providerID": "x",
               "tokens": {"total": total, "input": inp, "output": out,
                          "cache": {"read": read, "write": 0}}}
    if err:
        d["error"] = err
    if completed is not None:
        d["time"] = {"created": NOW - 1000, "completed": completed}
    if finish is not None:
        d["finish"] = finish
    return d


def test_default_db_uses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    got = odb.default_db()
    assert str(got) == str(tmp_path / ".local/share/opencode/opencode.db")


def test_session_usage_go_usd(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "go", cost=0.5, tin=100, tout=50, tr=14, cr=1000, cw=200)
    _ses(con, "usd", provider="openrouter", cost=0.25, tin=10, tout=5)
    _ses(con, "free", provider="openrouter", cost=0.0)
    con.commit()
    con.close()
    go = odb.session_usage("go", db)
    assert (go.tokens_in, go.tokens_out, go.tokens_reasoning) == (100, 50, 14)
    assert (go.cache_read, go.cache_write) == (1000, 200)
    assert (go.cost_go, go.cost_usd) == (0.5, 0.0)
    usd = odb.session_usage("usd", db)
    assert (usd.cost_go, usd.cost_usd) == (0.0, 0.25)
    free = odb.session_usage("free", db)
    assert (free.cost_go, free.cost_usd) == (0.0, 0.0)
    assert odb.session_usage("нет-такой", db) is None


def test_session_usage_context_skips_error_and_zero(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "s")
    _msg(con, "m_old", "s", _assist(total=61141, inp=524, read=60401), NOW - 90_000)
    _msg(con, "m_zero", "s", _assist(total=0, inp=0, read=0), NOW - 50_000)
    _msg(con, "m_err", "s",
          _assist(total=100, inp=10, read=20, err={"name": "MessageAbortedError"}),
          NOW - 10_000)
    con.commit()
    con.close()
    got = odb.session_usage("s", db)
    assert got.context == 524 + 60401


def test_session_usage_context_zero_when_no_live(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "s")
    _msg(con, "m", "s", {"role": "user", "tokens": {"total": 5}}, NOW)
    con.commit()
    con.close()
    assert odb.session_usage("s", db).context == 0


def test_session_state_pulse_tool_finished_usage(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "s", cost=0.5, tin=100, tout=50, tr=7, cr=1000, cw=200,
         t_created=NOW - 100_000, t_updated=NOW - 80_000)
    _msg(con, "m1", "s", _assist(total=100, inp=200, read=800,
                                 completed=NOW - 5_000, finish="stop"),
          NOW - 70_000)
    _part(con, "p_old", "m1", "s",
          {"type": "tool", "tool": "bash",
           "state": {"status": "completed", "input": {"command": "ls"},
                      "time": {"start": NOW - 60_000, "end": NOW - 59_000}}},
          NOW - 60_000)
    _part(con, "p_run", "m1", "s",
          {"type": "tool", "tool": "bash",
           "state": {"status": "running", "input": {"command": "pytest -q"},
                      "time": {"start": NOW - 25_000}}},
          NOW - 40_000)
    _todo(con, "s", NOW - 20_000)
    con.commit()
    con.close()
    st = odb.session_state("s", db)
    assert st.last_activity_ms == NOW - 20_000  # maximum: the todo is the newest
    assert st.active_tool == "bash"
    assert st.tool_started_ms == NOW - 25_000  # from state.time.start
    assert st.finished is True
    assert (st.usage.tokens_in, st.usage.cost_go) == (100, 0.5)
    assert st.usage.context == 200 + 800


def test_session_state_tool_time_fallback_and_not_finished(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "s", t_updated=NOW - 50_000)
    _msg(con, "m1", "s", _assist(total=10, inp=5, read=5), NOW - 40_000)
    # running without state.time.start → the time comes from part.time_created
    _part(con, "p", "m1", "s",
          {"type": "tool", "tool": "edit",
           "state": {"status": "running", "input": {}}},
          t=NOW - 30_000, t_created=NOW - 33_000)
    con.commit()
    con.close()
    st = odb.session_state("s", db)
    assert st.active_tool == "edit"
    assert st.tool_started_ms == NOW - 33_000
    assert st.finished is False  # no time.completed + finish


def test_session_state_idle_no_tool_no_assistant(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "s", t_updated=NOW - 50_000)
    _part(con, "p", "m", "s", {"type": "text", "text": "x"}, NOW - 40_000)
    con.commit()
    con.close()
    st = odb.session_state("s", db)
    assert st.active_tool == "" and st.tool_started_ms is None
    assert st.finished is False
    assert st.last_activity_ms == NOW - 40_000
    assert odb.session_state("нет", db) is None


def test_find_session(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "old", directory="/wt/T", t_created=NOW - 100_000, t_updated=NOW - 100_000)
    _ses(con, "new", directory="/wt/T", t_created=NOW - 10_000, t_updated=NOW - 10_000)
    _ses(con, "child", directory="/wt/T", t_created=NOW - 5_000,
         t_updated=NOW - 5_000, parent="new")
    _ses(con, "other", directory="/wt/X", t_created=NOW - 5_000, t_updated=NOW - 5_000)
    con.commit()
    con.close()
    assert odb.find_session("/wt/T", NOW - 60_000, db) == "new"  # child with parent_id skipped
    assert odb.find_session("/wt/X", NOW - 60_000, db) == "other"
    assert odb.find_session("/wt/T", NOW - 15_000, db) == "new"  # window is -5000
    assert odb.find_session("/wt/T", NOW - 4_000, db) is None  # new is older than the window, child with a parent is skipped
    assert odb.find_session("/wt/нет", 0, db) is None


def test_sessions_usage_batch_over_500(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    ids = [f"s{i:04d}" for i in range(605)]
    for i, sid in enumerate(ids):
        prov = "opencode-go" if i % 2 == 0 else "openrouter"
        con.execute(
            "INSERT INTO session (id, directory, cost, tokens_input, tokens_output,"
            " tokens_reasoning, tokens_cache_read, tokens_cache_write, model,"
            " time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "/wt/T", 0.01, 1, 2, 3, 4, 5,
             json.dumps({"id": "m", "providerID": prov}), NOW, NOW))
    con.commit()
    con.close()
    got = odb.sessions_usage(ids + ["missing"], db)
    assert len(got) == 605 and "missing" not in got
    assert (got["s0000"].cost_go, got["s0000"].cost_usd) == (0.01, 0.0)
    assert (got["s0001"].cost_go, got["s0001"].cost_usd) == (0.0, 0.01)
    assert got["s0000"].tokens_in == 1 and got["s0000"].context is None
    assert odb.sessions_usage([], db) == {}


def test_totals_bounds_and_empty(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "go_in", cost=0.5, tin=100, tout=50, tr=5, cr=10, cw=20,
         t_updated=NOW - 10_000)
    _ses(con, "usd_in", provider="openrouter", cost=0.25, tin=10, tout=5,
         t_updated=NOW - 20_000)
    _ses(con, "old", cost=9.0, tin=999, t_updated=NOW - 100_000)
    _ses(con, "new", provider="openrouter", cost=9.0, tin=999,
         t_updated=NOW + 100_000)
    con.commit()
    con.close()
    got = odb.totals(NOW - 50_000, db, NOW)
    assert (got.cost_go, got.cost_usd) == (0.5, 0.25)
    assert (got.tokens_in, got.tokens_out, got.tokens_reasoning) == (110, 55, 5)
    assert (got.cache_read, got.cache_write) == (10, 20)
    got2 = odb.totals(NOW - 50_000, db)  # no until — future rows count too
    assert got2.tokens_in == 110 + 999
    empty = odb.totals(NOW + 200_000, db)
    assert (empty.tokens_in, empty.cost_go, empty.cost_usd) == (0, 0.0, 0.0)


def test_unknown_schema_and_missing_file(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "s")
    con.commit()
    con.close()
    missing = tmp_path / "нет.db"
    assert odb.session_usage("s", missing) is None
    assert odb.sessions_usage(["s"], missing) == {}
    assert odb.session_state("s", missing) is None
    assert odb.find_session("/wt/T", 0, missing) is None
    z = odb.totals(0, missing)
    assert (z.tokens_in, z.cost_go, z.cost_usd) == (0, 0.0, 0.0)
    st = odb.check_schema(missing)
    assert st.ok is False and any("нет базы" in p for p in st.problems)

    # no table
    db2 = tmp_path / "без-таблицы.db"
    c2 = sqlite3.connect(str(db2))
    c2.executescript("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT);")
    c2.commit()
    c2.close()
    assert odb.session_usage("s", db2) is None
    assert odb.check_schema(db2).ok is False

    # own tables, foreign columns (session has no time_updated)
    db3 = tmp_path / "чужие-колонки.db"
    c3 = sqlite3.connect(str(db3))
    c3.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT,"
               " title TEXT, model TEXT, cost REAL, time_created INTEGER)")
    c3.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT,"
               " time_created INTEGER, time_updated INTEGER, data TEXT)")
    c3.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT,"
               " session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
    c3.execute("CREATE TABLE todo (session_id TEXT, content TEXT, status TEXT,"
               " priority TEXT, position INTEGER, time_created INTEGER, time_updated INTEGER)")
    c3.commit()
    c3.close()
    assert odb.session_usage("s", db3) is None
    assert odb.session_state("s", db3) is None
    assert odb.check_schema(db3).ok is False


def test_check_schema_ok(tmp_path):
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    con.commit()
    con.close()
    st = odb.check_schema(db)
    assert st.ok is True and st.problems == []


def test_real_home_not_touched():
    # Tests only touch an explicit path; the default is the HOME from conftest.
    home = Path(os.environ["HOME"])
    assert odb.default_db().is_relative_to(home)
