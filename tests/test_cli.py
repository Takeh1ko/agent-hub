"""CLI: status/roster/cost/import-legacy на фейковых данных."""

from __future__ import annotations

import json
import sqlite3

import hub.commands.cost as cost_cmd
import hub.commands.status as status_cmd
from hub.cli import main
from hub.store import Store

NOW = 1_789_000_000_000
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


def _oc(path, n=3):
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA)
    for i in range(n):
        prov = "opencode-go" if i % 2 == 0 else "openrouter"
        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"ses{i}", "p", f"/wt/T{i:02d}", f"т{i}",
             json.dumps({"id": "muse", "providerID": prov}),
             0.1, 0, 0, 0, 0, NOW - 30_000, NOW - 30_000))
    con.commit()
    con.close()


def _seed(store: Store, n=3):
    for i in range(n):
        store.upsert_task(id=f"T{i:02d}", stage="exec r1", round=1, updated_at=NOW)
        store.link_session(f"ses{i}", "opencode", f"T{i:02d}", "executor", 1, "muse")


def test_status_text_and_json(tmp_path, capsys, monkeypatch):
    _seed(Store())
    db = tmp_path / "oc.db"
    _oc(db)
    monkeypatch.setattr(status_cmd, "default_opencode_db", lambda: db)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "T00" in out
    assert main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["tasks"]) == 3


def test_status_limit_20(tmp_path, capsys, monkeypatch):
    store = Store()
    for i in range(20):
        store.upsert_task(id=f"T{i:02d}-длинное-название-задачи", stage="exec r1",
                          updated_at=NOW - i)
    db = tmp_path / "oc.db"
    _oc(db, 0)
    monkeypatch.setattr(status_cmd, "default_opencode_db", lambda: db)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert len(out.encode("utf-8")) <= 1500


def test_status_all_shows_merged(tmp_path, capsys, monkeypatch):
    store = Store()
    store.upsert_task(id="TA", stage="exec r1", updated_at=NOW)
    store.upsert_task(id="TM", stage="merged", updated_at=NOW)
    db = tmp_path / "oc.db"
    _oc(db, 0)
    monkeypatch.setattr(status_cmd, "default_opencode_db", lambda: db)
    main(["status"])
    assert "TM" not in capsys.readouterr().out
    main(["status", "--all"])
    assert "TM" in capsys.readouterr().out


def test_roster_and_cost(tmp_path, capsys, monkeypatch):
    _seed(Store())
    db = tmp_path / "oc.db"
    _oc(db)
    monkeypatch.setattr(status_cmd, "default_opencode_db", lambda: db)
    monkeypatch.setattr(cost_cmd, "DEFAULT_OPENCODB", db)
    assert main(["roster"]) == 0
    out = capsys.readouterr().out
    assert "T00" in out and "пишет код" in out  # сводка для владельца: этап словами
    for by in ("task", "model", "role", "day"):
        assert main(["cost", "--by", by]) == 0
        out = capsys.readouterr().out
        assert "Итого" in out and "go" in out
    assert main(["cost", "--since", "1д", "--by", "task"]) == 0


def test_cost_exact_groups(tmp_path, capsys, monkeypatch):
    """go/usd раздельно, группировка — точными строками, не словом «go»."""
    from datetime import datetime, timezone

    import hub.time as ht

    _seed(Store())  # ses0 go, ses1 usd, ses2 go; все executor/muse, цена 0.1
    db = tmp_path / "oc.db"
    _oc(db)
    monkeypatch.setattr(cost_cmd, "DEFAULT_OPENCODB", db)
    assert main(["cost", "--by", "model"]) == 0
    out = capsys.readouterr().out
    assert "muse: go $0.200 + usd $0.100" in out
    assert "Итого: go $0.200 + usd $0.100" in out
    assert main(["cost", "--by", "role"]) == 0
    out = capsys.readouterr().out
    assert "executor: go $0.200 + usd $0.100" in out
    assert main(["cost", "--by", "task"]) == 0
    out = capsys.readouterr().out
    assert "T00: go $0.100 + usd $0.000" in out
    assert "T01: go $0.000 + usd $0.100" in out
    assert main(["cost", "--by", "day"]) == 0
    out = capsys.readouterr().out
    day = datetime.fromtimestamp((NOW - 30_000) / 1000,
                                 tz=timezone.utc).astimezone(ht.TZ).strftime("%Y-%m-%d")
    assert f"{day}: go $0.200 + usd $0.100" in out


def test_cost_bad_since_returns_2(tmp_path, capsys, monkeypatch):
    db = tmp_path / "oc.db"
    _oc(db, 0)
    monkeypatch.setattr(cost_cmd, "DEFAULT_OPENCODB", db)
    assert main(["cost", "--since", "херня"]) == 2
    assert "непонятный --since" in capsys.readouterr().err


def test_import_legacy_cmd(tmp_path, capsys):
    wt = tmp_path / "wt"
    t = wt / "T09-x"
    (t / ".agent").mkdir(parents=True)
    (t / ".agent" / "state.json").write_text(json.dumps({
        "task": "T09-x", "base": "b", "worktree": str(t),
        "executor_session": "s", "reviewer_sessions": [],
        "round": 1, "verdicts": [], "status": "failed"}), encoding="utf-8")
    assert main(["import-legacy", "--worktrees", str(wt)]) == 0
    assert "T09-x" in capsys.readouterr().out
    # Без summary.md задача старого конвейера в работе (status=failed — его заглушка), а не провал.
    assert Store().get_task("T09-x")["stage"] == "exec r1"
