"""T164 model naming and reasoning effort as its own choice (legacy aliases kept).

Covers: canonical identity display, --effort validation per catalog, ALIAS[:EFFORT] in role
defaults and model switches, legacy mapping with the one-line notice (hidden from menus),
role menus storing alias + effort, plan column sizing and two-decimal prices, task/status level.
Fake catalogs only (no network).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import catalog as _catalog
from ahub import cli, registry, tasks, views
from ahub.model import Kind, Role
from ahub.providers.base import CatalogEntry, PlanKind
from ahub.store import Store
from tests.conftest import write


def _entry(alias="spark", provider="opencode", model_id="opencode-go/muse-spark-1.3-contributor",
           variant="xhigh"):
    return registry.ModelEntry(alias, provider, model_id, variant, True, "")


def _spark_catalog() -> dict[str, list[CatalogEntry]]:
    return {
        "opencode": [
            CatalogEntry("opencode-go/muse-spark-1.3-contributor", display_name="Muse Spark 1.3",
                         vendor="Meta", plan=PlanKind.GO, price_in=0.6, price_out=0.1,
                         context=1_000_000, reasoning=("minimal", "low", "medium", "high", "xhigh")),
        ],
        "agy": [
            CatalogEntry("gemini-3.8-flash-high", display_name="Gemini 3.8 Flash", vendor="Google",
                         plan=PlanKind.SUBSCRIPTION, reasoning=("high",)),
            CatalogEntry("gemini-3.8-flash-low", display_name="Gemini 3.8 Flash", vendor="Google",
                         plan=PlanKind.SUBSCRIPTION, reasoning=("low",)),
        ],
    }


@pytest.fixture
def store() -> Store:
    return Store()


def _en(monkeypatch):
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()


def test_split_and_legacy():
    assert registry.split_ref("spark:high") == ("spark", "high")
    assert registry.split_ref("spark") == ("spark", "")
    assert registry.base_alias("spark-high") == "spark"
    assert registry.stored_effort("spark-high") == "high"
    assert registry.stored_effort("spark:low") == "low"
    assert registry.stored_effort("spark") == ""
    assert registry.legacy_notice("spark-high") == "spark-high is spark:high"
    assert registry.legacy_notice("spark") == ""
    assert registry.model_ref("spark", "high") == "spark:high"
    assert registry.model_ref("spark", "") == "spark"


def test_legacy_hidden_from_menus_and_tables(store):
    assert "spark-high" in registry.LEGACY_ALIASES
    assert [e.alias for e, _ in registry.menu(store, Role.OBSERVER)] == ["spark", "spark"]
    entries = _catalog.visible_entries(registry.models(store))
    assert "spark-high" not in [e.alias for e in entries]
    assert "spark-medium" not in [e.alias for e in entries]
    assert "gemini-low" not in [e.alias for e in entries]
    assert "spark" in [e.alias for e in entries]


def test_valid_levels_from_catalog(monkeypatch):
    _en(monkeypatch)
    index = _catalog.index_by_id(_spark_catalog())
    spark = _entry()
    info = index["opencode-go/muse-spark-1.3-contributor"]
    assert "high" in registry.valid_levels(spark, info, index)
    assert "ultra" not in registry.valid_levels(spark, info, index)
    gemini = registry.ModelEntry("gemini", "agy", "gemini-3.8-flash-high", "", True, "")
    assert sorted(registry.valid_levels(gemini, index["gemini-3.8-flash-high"], index)) == ["high", "low"]
    with pytest.raises(registry.RegistryError, match="minimal"):
        registry.check_effort(spark, "ultra", info, index)
    with pytest.raises(registry.RegistryError):
        registry.check_effort(_entry("mimo-flash", "opencode", "opencode-go/mimo", ""), "high", None, {})


def test_identity_and_price_format(monkeypatch):
    _en(monkeypatch)
    index = _catalog.index_by_id(_spark_catalog())
    spark = _entry()
    info = index["opencode-go/muse-spark-1.3-contributor"]
    assert _catalog.identity_text(spark, info) == "Muse Spark 1.3 · Go plan · xhigh"
    assert _catalog.price_text(spark, info) == "$0.60 / $0.10"
    assert _catalog._plan_width() >= len("pay-as-you-go")
    _, _, maxw = _catalog.table_columns(200)
    assert maxw[3] >= len("pay-as-you-go")


def test_roles_show_effort(store, monkeypatch):
    _en(monkeypatch)
    roles = _catalog.roles_where_default(store, "spark")
    assert any(r.startswith("executor:") for r in roles)
    assert "executor:xhigh" in roles
    assert "observer:high" in roles


def test_task_new_effort_and_review_refs(store, monkeypatch, tmp_path):
    from ahub import config

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    proj = config.parse_project({"schema_version": 2, "name": "P", "allowed_paths": ["ahub/**"]}, str(tmp_path))
    spec = tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look", model="spark", effort="high")
    res = tasks.resolve(store, spec, proj, collect=False)
    assert (res.executor, res.effort) == ("spark", "high")
    bad = tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look", model="spark", effort="ultra")
    with pytest.raises(tasks.TaskInvalid, match="valid:"):
        tasks.resolve(store, bad, proj, collect=False)
    both = tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="look", model="spark:low", effort="high")
    with pytest.raises(tasks.TaskInvalid, match="disagree"):
        tasks.resolve(store, both, proj, collect=False)


def test_task_new_cli_effort_and_legacy_notice(monkeypatch, tmp_path, capsys):
    from ahub import paths
    from tests.enginekit import make_project

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)
    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n'
          f'worktrees = "{tmp_path / "wt"}"\nallowed_paths = ["core/**", "tests/**"]\n')
    write(paths.global_config_path(), f'projects = ["{project.root}"]\n')
    monkeypatch.chdir(str(project.root))
    assert cli.main(["task", "new", "--kind", "scout", "--title", "look",
                     "--model", "spark", "--effort", "high"]) == 0
    out = capsys.readouterr().out
    assert "spark:high" in out
    assert cli.main(["task", "new", "--kind", "scout", "--title", "legacy",
                     "--model", "spark-high"]) == 0
    out = capsys.readouterr().out
    assert "spark-high is spark:high" in out.splitlines()[0]
    assert cli.main(["task", "new", "--kind", "scout", "--title", "bad",
                     "--model", "spark", "--effort", "max"]) == 2


def test_models_role_set_default_with_effort(monkeypatch, capsys):
    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)
    assert cli.main(["models", "role", "observer", "--set-default", "spark:medium"]) == 0
    out = capsys.readouterr().out
    assert "spark:medium" in out
    assert cli.main(["--json", "models", "--role", "observer"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"alias": "spark", "effort": "medium", "ref": "spark:medium", "default": True} in \
        data["roles"]["observer"]
    assert cli.main(["models", "role", "observer", "--set-default", "spark:ultra"]) == 2


def test_model_switch_accepts_effort_and_legacy(monkeypatch, tmp_path, capsys):
    from ahub import paths
    from tests.enginekit import make_project

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n'
          f'worktrees = "{tmp_path / "wt"}"\nallowed_paths = ["core/**", "tests/**"]\n')
    write(paths.global_config_path(), f'projects = ["{project.root}"]\n')
    monkeypatch.chdir(str(project.root))
    store = Store()
    t = tasks.create(store, tasks.TaskSpec(project=project.name, kind=Kind.SCOUT, title="look"),
                     project, collect=False)
    assert cli.main(["model", t.label, "spark:low"]) == 0
    assert store.get_task(t.id).effort == "low"
    assert cli.main(["model", t.label, "spark-high"]) == 0
    out = capsys.readouterr().out
    assert "spark-high is spark:high" in out
    assert store.get_task(t.id).effort == "high"
    assert cli.main(["model", t.label, "spark:ultra"]) == 2


def test_status_shows_level(monkeypatch, tmp_path, capsys):
    from ahub import paths
    from tests.enginekit import make_project

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)
    project = make_project(tmp_path)
    write(Path(project.root) / ".hub.toml", 'schema_version = 2\nname = "P"\n'
          f'worktrees = "{tmp_path / "wt"}"\nallowed_paths = ["core/**", "tests/**"]\n')
    write(paths.global_config_path(), f'projects = ["{project.root}"]\n')
    monkeypatch.chdir(str(project.root))
    store = Store()
    t = tasks.create(store, tasks.TaskSpec(project=project.name, kind=Kind.SCOUT, title="look",
                                           model="spark", effort="high"),
                     project, collect=False)
    assert views.display_ref(store.get_task(t.id), store) == "spark:high"
    assert cli.main(["status", t.label]) == 0
    out = capsys.readouterr().out
    assert "spark:high" in out


def test_pick_effort_falls_back_past_denied_default(store, monkeypatch):
    """pick(role, effort) without an explicit alias skips a denied default like a plain pick does."""
    from ahub import config

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    denied = config.parse_project(
        {"schema_version": 2, "name": "P", "models": {"deny": ["muse-spark-1.3-contributor"]}}, "/tmp")
    picked = registry.pick(store, Role.SCOUT, denied, effort="high")
    assert (picked.alias, picked.variant) == ("deepseek-flash", "high")


def _v7_legacy_db(tmp_path):
    """A v7 database with legacy model/menu/task/session rows, as an old hub left them."""
    import sqlite3

    from ahub.store import MIGRATIONS_DIR, _split_sql

    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    for num in range(1, 8):
        f = next(p for p in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")) if int(p.name[:3]) == num)
        for stmt in _split_sql(f.read_text(encoding="utf-8")):
            con.execute(stmt)
    con.execute("PRAGMA user_version=7")
    con.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES"
                "('spark','opencode','opencode-go/muse-spark-1.3-contributor','xhigh',''),"
                "('spark-high','opencode','opencode-go/muse-spark-1.3-contributor','high',''),"
                "('spark-medium','opencode','opencode-go/muse-spark-1.3-contributor','medium',''),"
                "('gemini','agy','gemini-3.8-flash-high','',''),"
                "('gemini-low','agy','gemini-3.8-flash-low','','')")
    con.execute("INSERT INTO role_model(role, alias, position, is_default) VALUES"
                "('observer','spark-high',0,1),"
                "('observer','spark-medium',1,0),"
                "('scout','gemini-low',0,1)")
    con.execute("INSERT INTO task(project, kind, title, executor, created_at, updated_at) VALUES"
                "('P','scout','old','spark-high',0,0)")
    con.execute("INSERT INTO session(task_id, provider, external_id, role, round, model, started_at,"
                " log_path) VALUES(1,'agy','','scout',0,'gemini-low',0,'')")
    con.commit()
    con.close()
    return db


def test_migration_maps_legacy_menus(tmp_path, monkeypatch):
    """Migration 008 on real v7 legacy data: menus, task executors and sessions map to base + effort."""
    _en(monkeypatch)
    db = _v7_legacy_db(tmp_path)

    store = Store(path=db)
    assert store.schema_version() == 8
    refs = registry.menu_efforts(store, Role.OBSERVER)
    assert ("spark", "high", True) in refs
    assert ("spark", "medium", False) in refs
    assert registry.menu_efforts(store, Role.SCOUT) == [("gemini", "low", True)]
    assert all(a not in registry.HIDDEN_ALIASES for a, _e, _d in refs)
    t = store.get_task(1)
    assert (t.executor, t.effort) == ("spark", "high")
    s = store.list_sessions(1)[0]
    assert (s.model, s.effort) == ("gemini", "low")


def test_catalogs_cached_across_reads(monkeypatch):
    """get_catalogs shells out to every provider — repeated reads (status views) hit the cache."""
    from ahub.providers import agy as _agy
    from ahub.providers import codex as _codex
    from ahub.providers import opencode as _op

    calls: list[str] = []
    for cls, label in ((_op.OpencodeProvider, "opencode"), (_agy.AgyProvider, "agy"),
                       (_codex.CodexProvider, "codex")):
        def _wrap(self, refresh=False, _label=label):
            calls.append(_label)
            return []
        monkeypatch.setattr(cls, "catalog", _wrap)

    def _counts() -> list[int]:
        return sorted(calls.count(name) for name in ("agy", "codex", "opencode"))

    _catalog.reset_cache()
    _catalog.get_catalogs()
    assert _counts() == [1, 1, 1]
    _catalog.get_catalogs()
    assert _counts() == [1, 1, 1]  # cache hit: no new provider shells
    _catalog.get_catalogs(refresh=True)
    assert _counts() == [2, 2, 2]  # refresh re-fetches
    _catalog.reset_cache()
    _catalog.get_catalogs()
    assert _counts() == [3, 3, 3]


def test_providers_hides_legacy_but_keeps_rows(monkeypatch, capsys):
    """Legacy model rows stay in the DB (old tasks reference them) but never reach `ahub providers`."""
    from ahub import doctor

    _en(monkeypatch)
    store = Store()
    with store.tx() as c:
        c.execute("INSERT INTO model(alias, provider, model_id, variant, note) VALUES"
                  "('spark-high','opencode','opencode-go/muse-spark-1.3-contributor','high',''),"
                  "('spark-medium','opencode','opencode-go/muse-spark-1.3-contributor','medium',''),"
                  "('gemini-low','agy','gemini-3.8-flash-low','','')")
    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: [
        doctor.ProviderState("opencode", True, True, detail="d", note="n", hint=""),
        doctor.ProviderState("agy", True, True, detail="d", note="n", hint=""),
    ])
    monkeypatch.setattr("ahub.providers.agy.AgyProvider.quota", lambda self, force=False: [])
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: {})
    monkeypatch.setattr(_catalog, "_quota_pct_for", lambda entry, store=None: None)
    monkeypatch.setattr(_catalog, "_go_numbers", lambda: (None, None))
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)
    assert cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "spark-high" not in out and "spark-medium" not in out and "gemini-low" not in out
    assert "spark " in out and "gemini " in out
    with store.read() as c:
        kept = {r[0] for r in c.execute("SELECT alias FROM model")}
    assert {"spark-high", "spark-medium", "gemini-low"} <= kept


def test_reviewer_fallback_replaces_only_failing_ref(monkeypatch, tmp_path):
    """A quota failure on spark:high moves only that panel entry to the fallback, not spark:medium."""
    from ahub import engine, paths
    from ahub.model import State
    from ahub.providers.base import Outcome, RunResult
    from tests.enginekit import make_project

    _en(monkeypatch)
    monkeypatch.setattr(_catalog, "get_catalogs", lambda refresh=False: _spark_catalog())
    project = make_project(tmp_path)
    store = Store()
    t = tasks.create(store, tasks.TaskSpec(project=project.name, kind=Kind.REVIEW, title="look",
                                           review_models=["spark:high", "spark:medium"],
                                           review_input="core/a.py"),
                     project, collect=False)
    write(paths.global_config_path(), '[quota]\nfallback_reviewer = "bunny"\n')
    eng = engine.Engine(store, project, t.id, sleep=lambda s: None)
    state, _reason = eng._handle_quota_outcome(RunResult(Outcome.QUOTA, None, error="quota exhausted"),
                                               role=Role.REVIEWER, model_alias="spark:high")
    assert state is State.QUEUED
    rev = store.get_task(t.id).review
    assert rev["models"] == ["bunny", "spark"] and rev["efforts"] == ["", "medium"]


def test_console_model_usage_mentions_effort(monkeypatch):
    from ahub.i18n import t

    monkeypatch.setenv("AHUB_LANG", "en")
    from ahub.i18n import _reset

    _reset()
    assert "alias[:effort]" in t("console.model_usage")


def test_set_enabled_maps_legacy_alias(store, tmp_path, monkeypatch):
    """models disable spark-high disables the base spark row — fresh and migrated DBs alike."""
    _en(monkeypatch)
    registry.set_enabled(store, "spark-high", False)
    assert registry.get(store, "spark").enabled is False
    registry.set_enabled(store, "spark-high", True)
    assert registry.get(store, "spark").enabled is True

    migrated = Store(path=_v7_legacy_db(tmp_path))
    assert migrated.schema_version() == 8
    registry.set_enabled(migrated, "spark-high", False)
    assert registry.get(migrated, "spark").enabled is False


def test_role_remove_requires_effort_for_several(monkeypatch, capsys):
    """`models role observer --remove spark` with spark:high + spark:medium refuses listing both;
    --remove spark:medium drops one row; removing the last one is refused (menu_last)."""
    _en(monkeypatch)
    registry.seed(Store())  # menus exist (every real flow seeds via models/pick/get first)
    assert cli.main(["models", "role", "observer", "--remove", "spark"]) == 2
    err = capsys.readouterr().err
    assert "ALIAS:EFFORT" in err and "spark:high" in err and "spark:medium" in err
    store = Store()
    assert len(registry.menu_efforts(store, Role.OBSERVER)) == 2  # nothing removed
    assert cli.main(["models", "role", "observer", "--remove", "spark:medium"]) == 0
    assert registry.menu_efforts(Store(), Role.OBSERVER) == [("spark", "high", True)]
    assert cli.main(["models", "role", "observer", "--remove", "spark:high"]) == 2
    assert "at least one" in capsys.readouterr().err


def test_role_menu_effort_roundtrip(store):
    registry.add_to_role(store, Role.SCOUT, "spark:low")
    refs = dict(((a, e), d) for a, e, d in registry.menu_efforts(store, Role.SCOUT))
    assert ("spark", "low") in refs
    registry.set_default(store, Role.SCOUT, "spark:low")
    assert registry.role_default_ref(store, Role.SCOUT) == ("spark", "low")
    registry.remove_from_role(store, Role.SCOUT, "spark:low")
    assert ("spark", "low") not in dict(((a, e), d)
                                        for a, e, d in registry.menu_efforts(store, Role.SCOUT))
