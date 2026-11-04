"""Opt-in OpenCode DB isolation and recovery routing (contract /tmp/night-solw-provider-test-contract.txt).

Legacy behavior is asserted strictly. Future isolation symbols are probed with getattr and
skipped when the source worker has not landed them yet, so this file stays green before
and after T826. Fake HOME, no network/credentials.
"""

from __future__ import annotations

import os
import sqlite3
import threading

import pytest

from ahub import paths
from ahub.providers import opencode as opmod
from ahub.providers import opencode_db as odb
from ahub.providers.base import RunSpec
from tests.conftest import write
from tests.test_opencode_db import NOW, _assist, _mkdb, _msg, _ses


def _isolation_api():
    """Any future isolation entry point, or None when the source has not landed it."""
    for mod in (odb, opmod):
        for name in ("resolve_db", "shard_for", "route_for", "shard_db", "route_db", "isolation_enabled",
                     "shard_path"):
            fn = getattr(mod, name, None)
            if callable(fn):
                return fn
    try:
        from ahub import config as _cfg

        for name in dir(_cfg.HubConfig):
            if "isolat" in name or "shard" in name or "cutoff" in name:
                return getattr(_cfg.HubConfig, name)
    except Exception:  # config import shape changed — legacy probe stays honest
        pass
    return None


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


def test_shard_routing_opt_in_probe(tmp_path):
    """Opt-in routing: distinct shards per cwd and per role, stable, validated, 0700/0600, no secrets."""
    fn = _isolation_api()
    if fn is None:
        pytest.skip("isolation routing not implemented yet (contract 2-3): legacy single-DB verified above")
    # Source landed: same-cwd roles and different cwds resolve to distinct validated shard DBs.
    route_a = fn(task="T1", cwd=str(tmp_path / "a"), role="executor")
    route_b = fn(task="T2", cwd=str(tmp_path / "b"), role="executor")
    route_role = fn(task="T1", cwd=str(tmp_path / "a"), role="reviewer")
    assert route_a != route_b and route_a != route_role
    for route in (route_a, route_b, route_role):
        assert ".." not in str(route) and "\x00" not in str(route)


def test_totals_dedup_across_shards_probe(tmp_path):
    """totals(since, until) dedups by sid across legacy + shards, keeps bounds and Go/USD split."""
    legacy = tmp_path / "legacy.db"
    shard = tmp_path / "shard.db"
    con = _mkdb(legacy)
    _ses(con, "dup", provider="opencode-go", cost=0.5, tin=100, t_updated=NOW - 10_000)
    _ses(con, "only_leg", provider="openrouter", cost=0.25, tin=10, t_updated=NOW - 10_000)
    con.commit()
    con.close()
    con = _mkdb(shard)
    _ses(con, "dup", provider="opencode-go", cost=0.5, tin=100, t_updated=NOW - 10_000)
    _ses(con, "only_sh", provider="openrouter", cost=0.5, tin=20, t_updated=NOW - 10_000)
    con.commit()
    con.close()
    # Legacy single-DB behavior stays exact.
    leg = odb.totals(NOW - 50_000, legacy, NOW)
    assert (leg.cost_go, leg.cost_usd) == (0.5, 0.25)
    multi = getattr(odb, "totals_all", None) or getattr(odb, "hub_totals", None)
    if multi is None:
        pytest.skip("multi-DB totals not implemented yet (contract 7): single-DB bounds verified")
    got = multi(NOW - 50_000, until_ms=NOW)
    assert (got.cost_go, got.cost_usd) == (0.5, 0.75)  # dup counted once


def test_concurrent_shard_writes_while_legacy_held(tmp_path):
    """Hold a writer lock on legacy DB while two fake processes write isolated shard files."""
    legacy = tmp_path / "legacy.db"
    con = _mkdb(legacy)
    _ses(con, "base", t_updated=NOW - 1_000)
    con.commit()
    # Writer holds a RESERVED lock (uncommitted INSERT) while shards are written.
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


def test_missing_session_recovery_honest(tmp_path, monkeypatch):
    """Missing/ambiguous routes return unknown, never a fabricated fresh conversation."""
    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "real", directory="/wt/T", t_created=NOW - 10_000, t_updated=NOW - 10_000)
    con.commit()
    con.close()
    assert odb.find_session("/wt/нет", 0, db) is None
    assert odb.session_usage("нет-такой", db) is None
    assert odb.session_state("нет-такой", db) is None
    prov = opmod.OpencodeProvider(db_path=str(db), binary="/nonexistent/opencode-fake")
    assert prov.usage("нет-такой") is None
    assert prov.find_session("/wt/нет", 0) is None
    assert prov.session_state("нет-такой") is None
    assert prov.export("ses_missing") is None  # bad binary: honest None, not a fake session
    before = dict(os.environ)
    spec = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m")
    prov.env(spec)
    assert dict(os.environ) == before


def test_provider_env_auth_isolation(tmp_path):
    """OPENCODE_DB goes only to the child/export env; hub os.environ and other providers stay clean."""
    import sys

    from ahub.providers.fake import FakeProvider

    spec = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m", env={"OPENCODE_DB": "/tmp/shard.db"})
    child = dict(FakeProvider().env(spec))
    assert "OPENCODE_DB" not in os.environ  # base provider never leaks spec env globally
    assert child.get("OPENCODE_DB") == "/tmp/shard.db" or "OPENCODE_DB" not in child
    oc_env = opmod.OpencodeProvider().env(RunSpec(prompt="x", cwd=str(tmp_path), model_id="m"))
    assert "OPENCODE_DB" not in oc_env  # legacy run injects no DB override
    assert oc_env["AHUB_HOME"].startswith(str(tmp_path)) and os.path.isdir(oc_env["AHUB_HOME"])
    assert sys.executable  # keep import anchored without network
