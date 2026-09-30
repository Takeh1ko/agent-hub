from __future__ import annotations

import json

import pytest

from ahub import cli, config, registry
from ahub.model import Role
from ahub.store import Store
from tests.conftest import write


@pytest.fixture
def store() -> Store:
    return Store()


def _proj(deny=()):
    return config.parse_project({"schema_version": 2, "name": "PlayerUP", "models": {"deny": list(deny)}}, "/tmp")


def test_seed_once(store):
    assert registry.seed(store)
    assert not registry.seed(store)
    assert registry.get(store, "spark").model_id == registry.SPARK
    assert [e.alias for e, d in registry.menu(store, Role.EXECUTOR) if d] == ["spark"]
    assert [e.alias for e, _ in registry.menu(store, Role.SCOUT)] == ["spark", "deepseek-flash"]
    assert [e.alias for e, d in registry.menu(store, Role.OBSERVER) if d] == ["spark-high"]


def test_pick_default_and_explicit(store):
    assert registry.pick(store, Role.EXECUTOR, None).alias == "spark"
    assert registry.pick(store, Role.EXECUTOR, None, explicit="deepseek-flash").alias == "deepseek-flash"
    assert registry.pick(store, Role.EXECUTOR, None, explicit="spark-free").alias == "spark-free"  # вне меню — явно можно
    with pytest.raises(registry.RegistryError, match="нет модели"):
        registry.pick(store, Role.EXECUTOR, None, explicit="nope")


def test_project_deny_blocks_explicit_and_default(store):
    p = _proj(deny=["deepseek"])
    with pytest.raises(registry.RegistryError, match="запрещена в проекте PlayerUP"):
        registry.pick(store, Role.EXECUTOR, p, explicit="deepseek-flash")
    registry.set_default(store, Role.SCOUT, "deepseek-flash")
    assert registry.pick(store, Role.SCOUT, p).alias == "spark"  # умолчание запрещено — первая разрешённая
    assert registry.pick(store, Role.SCOUT, _proj()).alias == "deepseek-flash"  # в другом проекте доступна


def test_orchestrator_cannot_lift_project_deny(store):
    """Никакая ручка реестра не снимает запрет проекта: даже добавив свою модель с тем же id."""
    p = _proj(deny=["deepseek"])
    registry.add_model(store, "ds2", "opencode", "opencode-go/deepseek-v4.1-flash", "low")
    registry.add_to_role(store, Role.EXECUTOR, "ds2", default=True)
    with pytest.raises(registry.RegistryError):
        registry.pick(store, Role.EXECUTOR, p, explicit="ds2")
    assert registry.pick(store, Role.EXECUTOR, p).alias == "spark"


def test_disabled_and_empty_menu(store):
    registry.set_enabled(store, "spark-high", False)
    assert registry.pick(store, Role.OBSERVER, None).alias == "spark-medium"
    registry.set_enabled(store, "spark-medium", False)
    with pytest.raises(registry.RegistryError, match="нет доступной модели"):
        registry.pick(store, Role.OBSERVER, None)
    with pytest.raises(registry.RegistryError, match="выключена"):
        registry.check(store, "spark-high", None)


def test_menu_edits(store):
    registry.add_to_role(store, Role.SCOUT, "mimo-flash")
    assert [e.alias for e, _ in registry.menu(store, Role.SCOUT)] == ["spark", "deepseek-flash", "mimo-flash"]
    registry.remove_from_role(store, Role.SCOUT, "spark")  # был по умолчанию → умолчание переходит первому
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
    assert cli.main(["models", "--role", "executor"]) == 0
    out = capsys.readouterr().out
    assert "spark★" in out and "deepseek-flash(запрет проекта)" in out
    assert cli.main(["models", "role", "scout", "--add", "mimo-flash", "--default"]) == 0
    assert "mimo-flash★" in capsys.readouterr().out
    assert cli.main(["--json", "models", "--role", "scout"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {"alias": "mimo-flash", "default": True} in data["roles"]["scout"]
    assert cli.main(["models", "disable", "nope"]) == 2
    assert cli.main(["models", "--all"]) == 0
    assert "opencode-go/mimo-v2.6-flash" in capsys.readouterr().out
