"""Opt-in OpenCode DB isolation and recovery routing against T826 storage APIs.

Strict hermetic regression tests for contract /tmp/night-solw-provider-test-contract.txt.
Fake HOME, no credentials/network/live DB. Route identity comes from immutable RunSpec env.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading

from ahub import paths
from ahub.providers import opencode as opmod
from ahub.providers import opencode_db as odb
from ahub.providers import opencode_storage as storage
from ahub.providers.base import RunSpec
from tests.conftest import write
from tests.test_opencode_db import NOW, _assist, _mkdb, _msg, _ses


def _spec(cwd: str, route: str, isolate: bool = True, **extra: str) -> RunSpec:
    env = {"AHUB_OPENCODE_ROUTE": route, **extra}
    if isolate:
        env["AHUB_OPENCODE_ISOLATE"] = "1"
    return RunSpec(prompt="x", cwd=cwd, model_id="m", env=env)


def test_isolation_off_uses_legacy_db(tmp_path, monkeypatch):
    """Isolation OFF: explicit [paths].opencode_db wins, default is XDG/home, hub env untouched."""
    legacy = tmp_path / "legacy.db"
    con = _mkdb(legacy)
    _ses(con, "leg", directory="/wt/T", cost=0.5)
    con.commit()
    con.close()
    write(paths.global_config_path(), f'[paths]\nopencode_db = "{legacy}"\n')
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert odb.default_db() == legacy
    before = dict(os.environ)
    prov = opmod.OpencodeProvider(db_path=str(legacy))
    assert prov.usage("leg").cost_go == 0.5
    assert prov.find_session("/wt/T", NOW - 60_000) == "leg"
    assert dict(os.environ) == before  # telemetry never mutates the hub environment


def test_explicit_db_path_is_single_db(tmp_path):
    """An explicit db_path is the single-DB fixture/caller contract: totals stay single DB."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    for db, sid in ((db_a, "sa"), (db_b, "sb")):
        con = _mkdb(db)
        _ses(con, sid, cost=0.25, tin=10, t_updated=NOW - 1_000)
        con.commit()
        con.close()
    assert odb.totals(0, db_a).tokens_in == 10
    assert odb.totals(0, db_b).tokens_in == 10
    prov = opmod.OpencodeProvider(db_path=str(db_a))
    assert prov.usage("sa") is not None and prov.usage("sb") is None
    assert odb.sessions_usage(["sa", "sb"], db_a) == {"sa": odb.sessions_usage(["sa"], db_a)["sa"]}


def test_route_key_enrollment(tmp_path):
    """route_key is None until enrolled: isolate flag or existing metadata, never bare route."""
    cwd = str(tmp_path / "wt")
    assert storage.route_key(_spec(cwd, "")) is None  # no route at all
    bare = RunSpec(prompt="x", cwd=cwd, model_id="m", env={"AHUB_OPENCODE_ROUTE": "task:1:role:e:log:x"})
    assert storage.route_key(bare) is None  # bare route without isolate/metadata stays legacy
    assert storage.route_key(_spec(cwd, "task:1:role:e:log:x")) == "task:1:role:e:log:x"
    key = "task:9:role:executor:log:exec"
    assert storage.register(_spec(cwd, key), NOW) is not None  # enrollment persists metadata
    assert storage.has_route(key)
    assert storage.route_key(bare.__class__(prompt="x", cwd=cwd, model_id="m",
                                             env={"AHUB_OPENCODE_ROUTE": key})) == key


def test_register_distinct_stable_atomic(tmp_path, monkeypatch):
    """Different cwds and same-cwd roles get distinct shards; stable; 0700/0600; no secrets."""
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-secret-xyz")
    a = storage.register(_spec(str(tmp_path / "a"), "task:1:role:executor:log:exec"), NOW)
    b = storage.register(_spec(str(tmp_path / "b"), "task:2:role:executor:log:exec"), NOW)
    role = storage.register(_spec(str(tmp_path / "a"), "task:1:role:reviewer:log:rev"), NOW)
    assert a is not None and b is not None and role is not None
    assert a != b and a != role  # two simultaneous runs + two roles sharing cwd resolve distinctly
    again = storage.register(_spec(str(tmp_path / "a"), "task:1:role:executor:log:exec"), NOW + 60_000)
    assert again == a  # stable after restart; earliest started_after_ms kept
    meta = json.loads((_a_meta(a)).read_text(encoding="utf-8"))
    assert meta["started_after_ms"] == NOW
    root = paths.data_dir() / "opencode-shards"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for p in (root / f"{a.stem}.json", a):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600 if p.suffix != ".db" or p.exists() else True
    assert a.name.endswith(".db") and len(a.stem) == 64 and all(c in "0123456789abcdef" for c in a.stem)
    assert ".." not in str(a) and str(a).startswith(str(root))
    assert "sk-secret-xyz" not in (root / f"{a.stem}.json").read_text(encoding="utf-8")  # no secrets
    assert list(root.glob(".*.tmp.*")) == []  # atomic write leaves no tmp behind


def _a_meta(db_path):
    # Find the single metadata pointing at this db.
    for p in (paths.data_dir() / "opencode-shards").glob("*.json"):
        if json.loads(p.read_text(encoding="utf-8")).get("db") == str(db_path):
            return p
    raise AssertionError(f"no metadata for {db_path}")


def test_register_survives_worktree_removal(tmp_path):
    """Eligible route has no cwd in the key: removing the worktree keeps the same shard."""
    import shutil

    wt = tmp_path / "wt"
    wt.mkdir()
    key = "task:7:role:executor:log:exec"
    first = storage.register(_spec(str(wt), key), NOW)
    shutil.rmtree(wt)
    assert not wt.exists()
    second = storage.register(_spec(str(wt), key), NOW + 1_000)
    assert first == second and storage.db_for_spec(_spec(str(wt), key)) == first


def test_session_db_mapping_and_ambiguous(tmp_path):
    """session_db returns the single mapped shard, None when absent or ambiguous."""
    a = storage.register(_spec(str(tmp_path / "a"), "task:1:role:e:log:x"), NOW)
    b = storage.register(_spec(str(tmp_path / "b"), "task:2:role:e:log:y"), NOW)
    assert storage.session_db("ses-absent") is None
    storage.bind_session("ses_1", a)
    assert storage.session_db("ses_1") == a
    storage.bind_session("", a)  # empty sid is a no-op, never maps
    assert storage.session_db("") is None
    storage.bind_session("ses_1", b)  # same sid in two shards is ambiguous
    assert storage.session_db("ses_1") is None  # honest unknown, never a wrong shard


def test_totals_dedup_across_registered_shards(tmp_path):
    """totals(since, until) includes legacy + each shard once, dedups sids, keeps bounds/split."""
    legacy = tmp_path / "legacy.db"
    con = _mkdb(legacy)
    _ses(con, "dup", provider="opencode-go", cost=0.5, tin=100, t_updated=NOW - 10_000)
    _ses(con, "only_leg", provider="openrouter", cost=0.25, tin=10, t_updated=NOW - 10_000)
    _ses(con, "old", cost=9.0, tin=999, t_updated=NOW - 100_000)
    con.commit()
    con.close()
    write(paths.global_config_path(), "")  # no configured db: default routing reads legacy + shards
    monkeypatch_db = odb.default_db()
    assert str(monkeypatch_db) != str(legacy)  # default points at faked HOME, not our fixture
    # Point the default legacy DB at our fixture via XDG so _candidate_paths finds it first.
    import shutil

    monkeypatch_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(legacy, monkeypatch_db)
    key = "task:3:role:executor:log:exec"
    shard = storage.register(_spec(str(tmp_path / "wt"), key), NOW - 50_000)
    c2 = _mkdb(shard)
    _ses(c2, "dup", provider="opencode-go", cost=0.5, tin=100, t_updated=NOW - 10_000)
    _ses(c2, "only_sh", provider="openrouter", cost=0.5, tin=20, t_updated=NOW - 10_000)
    c2.commit()
    c2.close()
    # Explicit totals stay single-DB.
    assert odb.totals(NOW - 50_000, legacy, NOW).tokens_in == 110
    got = odb.totals(NOW - 50_000, None, NOW)
    assert (got.cost_go, got.cost_usd) == (0.5, 0.75)  # dup counted once, provider split kept
    assert got.tokens_in == 130
    assert odb.totals(NOW, None, NOW + 1).tokens_in == 0  # upper bound holds


def test_find_session_registered_shard_recovery(tmp_path):
    """Crash before SESSION still recovers via the registered shard within tolerance."""
    wt = str(tmp_path / "wt")
    key = "task:5:role:executor:log:exec"
    shard = storage.register(_spec(wt, key, started_ms=NOW - 60_000), NOW - 60_000)
    con = _mkdb(shard)
    _ses(con, "shard_ses", directory=wt, t_created=NOW - 10_000, t_updated=NOW - 10_000)
    con.commit()
    con.close()
    prov = opmod.OpencodeProvider(binary="/nonexistent/opencode-fake")
    spec = _spec(wt, key)
    found = prov.find_session_for_run(wt, NOW - 60_000, spec)
    assert found == "shard_ses"  # no unbounded scan: default candidates are legacy + registered shards
    assert prov.find_session_for_run("/wt/нет", 0, spec) is None
    # Legacy exact-cwd/parent filter/tolerance still hold through the same path.
    assert odb.find_session("/wt/нет", 0, shard) is None


def test_lock_narrow_same_vs_unrelated(tmp_path):
    """Same shard serializes; unrelated shards stay parallel; lock file is 0600."""
    import time

    a1 = _spec(str(tmp_path / "a"), "task:1:role:e:log:x")
    a2 = _spec(str(tmp_path / "a"), "task:1:role:e:log:x")
    other = _spec(str(tmp_path / "b"), "task:2:role:e:log:y")
    with storage.lock(a1):
        db = storage.register(a1, int(time.time() * 1000))
        lock_path = db.with_suffix(".lock")
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
    order: list[str] = []
    started = threading.Event()
    done = threading.Event()

    def _bg():
        started.set()
        with storage.lock(a2):
            order.append("bg")
        done.set()

    with storage.lock(a1):
        t = threading.Thread(target=_bg)
        t.start()
        assert started.wait(5)
        time.sleep(0.3)
        assert order == []  # same namespace blocks
        with storage.lock(other):  # unrelated namespace never blocks
            order.append("other")
        assert order == ["other"]
    assert done.wait(10)  # outer lock released, the waiter proceeds
    t.join(timeout=10)
    assert not t.is_alive() and order == ["other", "bg"]


def test_concurrent_shard_writes_while_legacy_held(tmp_path):
    """Hold a writer lock on legacy DB while two fake processes write isolated shard files."""
    legacy = tmp_path / "legacy.db"
    con = _mkdb(legacy)
    _ses(con, "base", t_updated=NOW - 1_000)
    con.commit()
    holder = sqlite3.connect(str(legacy), timeout=30)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO session (id, directory, cost, model, time_created, time_updated)"
                   " VALUES (?,?,?, ?,?,?)",
                   ("held", "/wt/T", 0.0, "{}", NOW, NOW))
    errors: list[str] = []

    def _write_shard(name: str, sid: str) -> None:
        try:
            db = tmp_path / name
            c = _mkdb(db)
            _ses(c, sid, directory=f"/wt/{sid}", cost=0.1, tin=7, t_updated=NOW)
            _msg(c, f"m_{sid}", sid, _assist(total=10, inp=5, read=5))
            c.commit()
            c.close()
        except sqlite3.Error as e:  # narrow: only sqlite contention may fail here
            errors.append(f"{name}: {e}")

    threads = [threading.Thread(target=_write_shard, args=(f"shard{i}.db", f"s{i}")) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    holder.rollback()
    holder.close()
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    for i in range(2):
        got = odb.session_usage(f"s{i}", tmp_path / f"shard{i}.db")
        assert got is not None and got.tokens_in == 7  # row aggregates, context from messages
        assert got.context == 10


def test_missing_session_recovery_honest(tmp_path):
    """Missing/ambiguous routes return unknown, never a fabricated fresh conversation."""
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "real", directory="/wt/T", t_created=NOW - 10_000, t_updated=NOW - 10_000)
    con.commit()
    con.close()
    assert odb.find_session("/wt/нет", 0, db) is None
    assert odb.session_usage("нет-такой", db) is None
    assert odb.session_state("нет-такой", db) is None
    assert storage.session_db("нет-такой") is None
    prov = opmod.OpencodeProvider(db_path=str(db), binary="/nonexistent/opencode-fake")
    assert prov.usage("нет-такой") is None
    assert prov.find_session("/wt/нет", 0) is None
    assert prov.session_state("нет-такой") is None
    assert prov.export("ses_missing") is None  # bad binary: honest None, not a fake session
    before = dict(os.environ)
    prov.env(_spec(str(tmp_path), "task:1:role:e:log:x"))
    assert dict(os.environ) == before


def test_provider_env_strict_routing(tmp_path, monkeypatch):
    """OPENCODE_DB goes only to the child env; explicit/spec/config precedence is exact."""
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    prov = opmod.OpencodeProvider()
    legacy_spec = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m")
    assert "OPENCODE_DB" not in prov.env(legacy_spec)  # legacy run injects no override
    routed = storage.register(_spec(str(tmp_path), "task:1:role:e:log:x"), NOW)
    child = prov.env(_spec(str(tmp_path), "task:1:role:e:log:x"))
    assert child["OPENCODE_DB"] == str(routed)  # routed child gets its shard
    explicit = prov.env(RunSpec(prompt="x", cwd=str(tmp_path), model_id="m",
                                env={"OPENCODE_DB": "/tmp/keep.db", "AHUB_OPENCODE_ROUTE": "task:1:role:e:log:x",
                                     "AHUB_OPENCODE_ISOLATE": "1"}))
    assert explicit["OPENCODE_DB"] == "/tmp/keep.db"  # spec OPENCODE_DB wins, no re-register
    assert "OPENCODE_DB" not in os.environ  # hub environment never mutated
    single = opmod.OpencodeProvider(db_path="/tmp/explicit.db").env(_spec(str(tmp_path), "task:1:role:e:log:x"))
    assert single["OPENCODE_DB"] == "/tmp/explicit.db"  # explicit db_path beats routing
    write(paths.global_config_path(), f'[paths]\nopencode_db = "{tmp_path / "cfg.db"}"\n')
    cfg_env = prov.env(_spec(str(tmp_path), "task:9:role:e:log:z"))
    assert cfg_env["OPENCODE_DB"] == str(tmp_path / "cfg.db")  # configured db wins, shard suppressed
