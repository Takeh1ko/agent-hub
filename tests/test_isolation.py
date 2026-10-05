"""The suite never touches the live hub: tripwires around the live paths (T151).

HOME/AHUB_HOME are faked per test, but a child process (worker, gates, service) or a path that
misses the fakes would otherwise write live rows silently. While AHUB_UNDER_TEST=1, opening a hub
path under the real home fails loudly — in this process and in every child (env is inherited).
AHUB_TEST_REAL_HOME points the check at a temp dir, so the tests below touch nothing real.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ahub import paths
from ahub.store import Store

REPO = Path(__file__).resolve().parents[1]
# getattr fallbacks: on code without the tripwire the tests fail (no raise), not error on import.
UNDER_TEST = getattr(paths, "UNDER_TEST", "AHUB_UNDER_TEST")
TEST_REAL_HOME = getattr(paths, "TEST_REAL_HOME", "AHUB_TEST_REAL_HOME")


def test_live_db_open_refused(tmp_path, monkeypatch):
    home = tmp_path / "realhome"
    db = home / ".local/share/ahub/ahub.db"
    Store(db)  # hermetic tmp path, the check is not yet aimed at it — creates the DB
    assert db.exists()
    monkeypatch.setenv(UNDER_TEST, "1")  # conftest already sets it; keep it explicit
    monkeypatch.setenv(TEST_REAL_HOME, str(home))
    with pytest.raises(RuntimeError, match="refusing live hub DB"):
        Store(db)
    with pytest.raises(RuntimeError, match="refusing live hub DB"):
        sqlite3.connect(str(db))
    with pytest.raises(RuntimeError, match="refusing live hub DB"):
        sqlite3.connect(f"file:{db}?mode=rw", uri=True)
    # the session guard reads the live DB read-only — that stays allowed
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert con.execute("SELECT count(*) FROM task").fetchone()[0] == 0
    finally:
        con.close()


def test_plain_open_works_without_the_flag(tmp_path, monkeypatch):
    """No flag — production behaviour unchanged (the tripwire is test-only)."""
    home = tmp_path / "realhome"
    monkeypatch.delenv(UNDER_TEST, raising=False)
    monkeypatch.setenv(TEST_REAL_HOME, str(home))
    assert Store(home / ".local/share/ahub/ahub.db").schema_version() >= 1


def test_child_process_cannot_open_live_db(tmp_path):
    """A fresh interpreter with the test flag refuses the live hub DB (worker/accept/service children)."""
    home = tmp_path / "childhome"
    env = dict(os.environ)
    for var in ("AHUB_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                "AHUB_FAKE_QUEUE", "AHUB_FAKE_PROVIDER"):
        env.pop(var, None)
    env["HOME"] = str(home)
    env[UNDER_TEST] = "1"
    env[TEST_REAL_HOME] = str(home)
    env["PYTHONPATH"] = str(REPO)
    code = "from ahub.store import Store; Store(); print('opened')"
    r = subprocess.run([sys.executable, "-c", code], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    assert "refusing live hub DB" in r.stderr


def test_session_backstop_reports_test_shaped_rows(tmp_path, monkeypatch):
    """The session backstop names new live 'P' rows and lock touches (attribution by content)."""
    from tests.conftest import _live_traces, _lock_stat

    home = tmp_path / "livehome"
    db = home / ".local/share/ahub/ahub.db"
    p_lock = home / ".local/share/ahub/accept-P.lock"
    monkeypatch.delenv(UNDER_TEST, raising=False)  # disarm tripwires to plant rows hermetically
    store = Store(db)
    tid = store.create_task(project="P", kind="scout", title="planted")
    from ahub import transitions
    from ahub.model import State

    transitions.move(store, tid, State.PREPARING, now=5)
    traces = _live_traces(db, p_lock, (0, 0, 0, 0, 0), 0, 0, True, None)
    assert any("task" in t and "planted" in t for t in traces)
    assert any(t.startswith("event:") for t in traces)
    p_lock.write_text("T1\n")
    only_lock = _live_traces(db, p_lock, (0, 0, 0, 0, 0), 10 ** 9, 10 ** 9, True, None)
    assert only_lock == ["accept-P.lock created in the live hub dir"]
    assert _live_traces(db, p_lock, (0, 0, 0, 0, 0), 10 ** 9, 10 ** 9, True,
                        _lock_stat(p_lock)) == []


def test_child_envs_keep_isolation(tmp_path):
    """Every place that builds a child env keeps the isolation vars (audit of the T151 leak)."""
    from ahub import prepare
    from ahub.providers import fake as fake_mod
    from ahub.providers.base import RunSpec
    from ahub.providers.codex import CodexProvider
    from ahub.providers.opencode import OpencodeProvider
    from ahub.selfupdate import hub_env

    faked = {"HOME": str(tmp_path), "AHUB_HOME": str(tmp_path / "ahub-home"),
             "XDG_DATA_HOME": str(tmp_path / "xdg-data"), "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
             "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"), "AHUB_FAKE_QUEUE": str(tmp_path / "q"),
             "PYTHONPATH": "/repo", "SOME_TOKEN": "secret"}
    scrubbed = prepare.scrub_env(dict(faked))
    for var in ("HOME", "AHUB_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
                "AHUB_FAKE_QUEUE", "PYTHONPATH"):
        assert scrubbed[var] == faked[var]
    assert "SOME_TOKEN" not in scrubbed  # secrets still go
    assert prepare.apply_proxy(dict(faked), None, None) == faked

    kept = hub_env()
    assert kept["AHUB_HOME"] == os.environ["AHUB_HOME"] and kept["HOME"] == os.environ["HOME"]

    spec = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m")
    merged = dict(faked)
    merged.update(fake_mod.FakeProvider().env(spec))
    assert merged["AHUB_HOME"] == faked["AHUB_HOME"]  # the fake agent shares the test hub
    for prov in (OpencodeProvider(), CodexProvider()):
        merged = dict(faked)
        merged.update(prov.env(spec))
        assert merged["AHUB_HOME"] == str(Path(spec.cwd) / ".ahub" / "home")  # its own copy, not live
        assert merged["HOME"] == faked["HOME"]
