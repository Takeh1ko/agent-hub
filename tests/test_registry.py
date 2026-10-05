from __future__ import annotations

import json

import pytest

from ahub import cli, config, paths, registry
from ahub.model import Role
from ahub.store import Store
from tests.conftest import write


@pytest.fixture
def store() -> Store:
    return Store()


def _proj(deny=()):
    return config.parse_project({"schema_version": 2, "name": "webapp", "models": {"deny": list(deny)}}, "/tmp")


def test_fake_provider_from_env(store, monkeypatch):
    """AHUB_FAKE_PROVIDER=1 — the fake provider is selectable from the CLI (tools/smoke.sh)."""
    assert registry.pick(store, Role.SCOUT, None).alias == "spark"  # without the flag
    monkeypatch.setenv("AHUB_FAKE_PROVIDER", "1")
    assert registry.get(store, "fake").provider == "fake"
    for role in Role:
        assert registry.pick(store, role, None).alias == "fake"
        assert ("fake" in [e.alias for e, _ in registry.menu(store, role)])
    assert registry.check(store, "fake", None).model_id == "fake/model"
    monkeypatch.delenv("AHUB_FAKE_PROVIDER", raising=False)
    assert registry.pick(store, Role.SCOUT, None).alias == "spark"
    for role in Role:
        assert registry.pick(store, role, None).alias != "fake"


def test_seed_once(store):
    assert registry.seed(store)
    assert not registry.seed(store)
    assert registry.get(store, "spark").model_id == registry.SPARK
    assert [e.alias for e, d in registry.menu(store, Role.EXECUTOR) if d] == ["spark"]
    assert [e.alias for e, _ in registry.menu(store, Role.SCOUT)] == ["spark", "deepseek-flash"]
    default = next(e for e, d in registry.menu(store, Role.OBSERVER) if d)
    assert (default.alias, default.variant) == ("spark", "high")
    assert [(e.alias, e.variant) for e, _ in registry.menu(store, Role.OBSERVER)] == [
        ("spark", "high"), ("spark", "medium")]


def test_pick_default_and_explicit(store):
    assert registry.pick(store, Role.EXECUTOR, None).alias == "spark"
    assert registry.pick(store, Role.EXECUTOR, None, explicit="deepseek-flash").alias == "deepseek-flash"
    outside = registry.pick(store, Role.EXECUTOR, None, explicit="spark-free")  # outside the menu — explicit is fine
    assert outside.alias == "spark-free"
    with pytest.raises(registry.RegistryError, match="нет модели"):
        registry.pick(store, Role.EXECUTOR, None, explicit="nope")


def test_project_deny_blocks_explicit_and_default(store):
    p = _proj(deny=["deepseek"])
    with pytest.raises(registry.RegistryError, match="запрещена в проекте webapp"):
        registry.pick(store, Role.EXECUTOR, p, explicit="deepseek-flash")
    registry.set_default(store, Role.SCOUT, "deepseek-flash")
    assert registry.pick(store, Role.SCOUT, p).alias == "spark"  # the default is denied — first allowed one
    assert registry.pick(store, Role.SCOUT, _proj()).alias == "deepseek-flash"  # allowed in another project


def test_orchestrator_cannot_lift_project_deny(store):
    """No registry knob lifts a project deny: not even by adding your own model with the same id."""
    p = _proj(deny=["deepseek"])
    registry.add_model(store, "ds2", "opencode", "opencode-go/deepseek-v4.1-flash", "low")
    registry.add_to_role(store, Role.EXECUTOR, "ds2", default=True)
    with pytest.raises(registry.RegistryError):
        registry.pick(store, Role.EXECUTOR, p, explicit="ds2")
    assert registry.pick(store, Role.EXECUTOR, p).alias == "spark"


def test_disabled_and_empty_menu(store):
    registry.set_enabled(store, "mimo-flash", False)
    assert registry.pick(store, Role.EXECUTOR, None).alias == "spark"
    registry.set_enabled(store, "spark", False)
    assert registry.pick(store, Role.EXECUTOR, None).alias == "deepseek-flash"
    registry.set_enabled(store, "deepseek-flash", False)
    with pytest.raises(registry.RegistryError, match="нет доступной модели"):
        registry.pick(store, Role.EXECUTOR, None)
    with pytest.raises(registry.RegistryError, match="выключена"):
        registry.check(store, "spark", None)


def test_menu_edits(store):
    registry.add_to_role(store, Role.SCOUT, "mimo-flash")
    assert [e.alias for e, _ in registry.menu(store, Role.SCOUT)] == ["spark", "deepseek-flash", "mimo-flash"]
    registry.remove_from_role(store, Role.SCOUT, "spark")  # it was the default → the default goes to the first one
    assert [e.alias for e, d in registry.menu(store, Role.SCOUT) if d] == ["deepseek-flash"]
    registry.remove_from_role(store, Role.SCOUT, "mimo-flash")
    with pytest.raises(registry.RegistryError, match="хотя бы одна"):
        registry.remove_from_role(store, Role.SCOUT, "deepseek-flash")
    with pytest.raises(registry.RegistryError, match="сначала добавьте"):
        registry.set_default(store, Role.SCOUT, "spark")
    with pytest.raises(registry.RegistryError, match="уже есть"):
        registry.add_model(store, "spark", "opencode", "x")


def test_cli_models(tmp_path, monkeypatch, capsys):
    write(tmp_path / "p" / ".hub.toml", 'schema_version = 2\nname = "P"\n[models]\ndeny = ["deepseek"]\n')
    monkeypatch.chdir(tmp_path / "p")
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)  # wide: no column is clipped
    assert cli.main(["models", "--role", "executor"]) == 0
    out = capsys.readouterr().out
    # the catalog table, grouped by provider: alias, model, reasoning, plan, price, roles
    assert "алиас" in out and "уровень" in out and "план" in out and "роли" in out
    assert "spark" in out and "deepseek-flash(запрет проекта)" in out
    assert cli.main(["models", "role", "scout", "--add", "mimo-flash", "--default"]) == 0
    assert "mimo-flash" in capsys.readouterr().out
    assert cli.main(["--json", "models", "--role", "scout"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"alias": "mimo-flash", "effort": "", "ref": "mimo-flash", "default": True} in data["roles"]["scout"]
    assert cli.main(["models", "disable", "nope"]) == 2
    assert cli.main(["models", "--all"]) == 0
    assert "opencode-go/mimo-v2.6-flash" in capsys.readouterr().out


def test_cli_models_says_a_broken_global_config(capsys, monkeypatch, tmp_path):
    """T107: a config in the wrong encoding must not hide the models silently — one line, no traceback."""
    from ahub.i18n import _reset

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    monkeypatch.setattr("ahub.ui.width", lambda explicit=None: 200)  # wide: no column is clipped
    p = paths.global_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'lang = "\xff\xfe"')  # the bytes a cp1251 editor leaves behind
    assert cli.main(["models", "--role", "executor"]) == 0
    out = capsys.readouterr().out
    assert "executor" in out and "spark" in out  # the menu is still there
    assert f"error: {p}: not UTF-8 text" in out  # and the reason is on the same screen
    assert cli.main(["--json", "models", "--role", "executor"]) == 0
    assert str(p) in json.loads(capsys.readouterr().out)["config_error"]


def test_free_candidates_order(store):
    """The probe tries the free aliases in the registry order: spark-free, then bunny."""
    assert registry.get(store, "bunny").model_id == "opencode/space-bunny-free"
    assert [e.alias for e in registry.free_candidates(store)][:2] == ["spark-free", "bunny"]
    assert registry.is_free(registry.get(store, "bunny")) and not registry.is_free(registry.get(store, "spark"))
    registry.set_enabled(store, "spark-free", False)
    assert registry.free_candidates(store)[0].alias == "bunny"


def test_seed_adds_defaults_missing_in_an_old_hub(store):
    """An existing hub picks up a new default alias on upgrade: enabled, but in no role menu."""
    registry.seed(store)
    with store.tx() as c:
        c.execute("DELETE FROM model WHERE alias='bunny'")
    assert registry.seed(store) is False  # the registry is not empty, so nothing is seeded from scratch
    assert registry.get(store, "bunny").model_id == "opencode/space-bunny-free"
    assert all("bunny" not in [e.alias for e, _ in registry.menu(store, role)] for role in Role)


def test_cli_models_check_probes(capsys, monkeypatch):
    """`ahub models check` — a line per alias, exit 1 when one of them does not answer."""
    from ahub import doctor

    tried: list[str] = []

    def _probe(entry, timeout_s=60):
        tried.append(entry.alias)
        ok = entry.alias != "spark"
        return ok, f"{entry.alias}: {'ответил' if ok else 'молчит'}"

    monkeypatch.setattr(doctor, "probe_model", _probe)
    assert cli.main(["models", "check", "bunny", "spark-free"]) == 0
    assert tried == ["bunny", "spark-free"]
    assert capsys.readouterr().out.splitlines() == [
        "     модель      проба",
        "  ✓  bunny       ответил",
        "  ✓  spark-free  ответил",
        "2 из 2 моделей отвечают",
    ]
    assert cli.main(["--json", "models", "check", "spark"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["checked"] == [{"alias": "spark", "ok": False, "detail": "spark: молчит"}]
    # no aliases — the role defaults (executor spark, observer spark:high)
    assert cli.main(["models", "check"]) == 1
    assert tried[-2:] == ["spark", "spark"]
    assert cli.main(["models", "check", "nope"]) == 2


def test_role_default_raises_on_db_error(store, monkeypatch):
    """Finding 9: role_default must raise on DB failure instead of swallowing it and returning None."""
    import sqlite3

    def _broken_read():
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(store, "read", _broken_read)
    with pytest.raises(sqlite3.DatabaseError):
        registry.role_default(store, Role.EXECUTOR)


def test_disabled_providers_mtime_cached(tmp_path, monkeypatch):
    """Finding 11: disabled_providers caches by mtime and does not re-parse on every query."""
    calls = 0
    orig_load = config.load_hub

    def _counting_load(*a, **k):
        nonlocal calls
        calls += 1
        return orig_load(*a, **k)

    monkeypatch.setattr(registry, "load_hub", _counting_load)
    cfg_file = paths.global_config_path()
    write(cfg_file, '[providers.codex]\nenabled = false\n')

    # first call parses
    res1 = registry.disabled_providers()
    assert "codex" in res1
    count1 = calls

    # second call with untouched file hits cache
    res2 = registry.disabled_providers()
    assert res2 == res1
    assert calls == count1

    # file change updates cache
    import time
    time.sleep(0.01)
    write(cfg_file, '[providers.agy]\nenabled = false\n')
    res3 = registry.disabled_providers()
    assert "agy" in res3
    assert "codex" not in res3
    assert calls > count1

