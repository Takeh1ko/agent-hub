"""ahub providers: the table, --json, and the on/off switch (a disabled provider offers no models)."""

from __future__ import annotations

import json

import pytest

from ahub import cli, config, doctor, paths, registry, tasks
from ahub.model import Kind, Role
from ahub.store import Store
from tests.conftest import write
from tests.enginekit import make_project


def _states(monkeypatch, **kw) -> list[doctor.ProviderState]:
    """Fake provider states: opencode and agy found and logged in, codex missing (unless overridden)."""
    states = {
        "opencode": doctor.ProviderState("opencode", True, True, detail="found /bin/opencode",
                                         note="opencode-go: платный Spark доступен", hint=""),
        "agy": doctor.ProviderState("agy", True, True, detail="found /bin/agy",
                                    note="Gemini через Antigravity, квота окна (без денег)", hint=""),
        "codex": doctor.ProviderState("codex", False, False, detail="not found",
                                      note="использует ваш план ChatGPT",
                                      hint="поставить codex: npm i -g @openai/codex, затем codex login"),
    }
    states.update(kw)
    out = [states[n] for n in ("opencode", "agy", "codex") if n in states]
    monkeypatch.setattr(doctor, "provider_states", lambda *a, **k: list(out))
    return out


def _rows(out: str) -> list[str]:
    """The table rows (the note and hint lines are indented)."""
    return [ln for ln in out.splitlines() if ln.strip() and not ln.startswith(" ")]


def _on(name: str) -> bool:
    return config.load_hub().provider_enabled(name)


def test_providers_table_shows_every_provider(capsys, monkeypatch):
    _states(monkeypatch)
    assert cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    rows = _rows(out)
    assert rows[0].split() == ["имя", "найден", "вход", "включён", "модели"]
    assert rows[1].split()[:4] == ["opencode", "\u2713", "\u2713", "включён"]
    assert "spark" in rows[1] and "mimo-flash" in rows[1]
    assert rows[2].split()[:4] == ["agy", "\u2713", "\u2713", "включён"]
    assert "gemini" in rows[2]
    # codex is not found: the mark, the note and the one-line install hint
    assert rows[3].split()[:4] == ["codex", "\u2717", "\u2013", "включён"]
    assert "codex, codex-fast" in rows[3]
    assert "npm i -g @openai/codex" in out
    # nothing is switched off yet — the switch is the hub config, and it is not touched by a view
    assert all(_on(n) for n in ("opencode", "agy", "codex"))
    assert not paths.global_config_path().exists()


def test_providers_json(capsys, monkeypatch):
    _states(monkeypatch, codex=doctor.ProviderState("codex", False, False, detail="not found", note="n",
                                                    hint="поставить codex"))
    assert cli.main(["--json", "providers"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [p["name"] for p in data["providers"]] == ["opencode", "agy", "codex"]
    first = data["providers"][0]
    assert set(first) == {"name", "found", "logged_in", "enabled", "detail", "note", "hint", "models"}
    assert first["enabled"] is True and "spark" in first["models"]
    codex = data["providers"][2]
    assert codex["found"] is False and codex["logged_in"] is False and codex["enabled"] is True


def test_disable_codex_refuses_a_task_with_its_model(capsys, monkeypatch, tmp_path):
    _states(monkeypatch)
    assert cli.main(["providers", "disable", "codex"]) == 0
    assert "codex" in capsys.readouterr().out
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "[providers.codex]" in text and "enabled = false" in text
    assert _on("codex") is False
    store = Store()
    # the models stay in the registry, they are just not offered
    assert "codex" in [e.alias for e in registry.models(store)]
    assert "codex" not in [e.alias for e, _ in registry.menu(store, Role.EXECUTOR)]
    with pytest.raises(registry.RegistryError, match="ahub providers enable codex"):
        registry.check(store, "codex", None)
    project = make_project(tmp_path)
    with pytest.raises(tasks.TaskInvalid) as ei:
        tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="разведка", model="codex"),
                     project, collect=False)
    assert "ahub providers enable codex" in " | ".join(ei.value.errors)
    # back on — and the task goes through
    assert cli.main(["providers", "enable", "codex"]) == 0
    capsys.readouterr()
    assert _on("codex") is True
    task = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="разведка", model="codex"),
                        project, collect=False)
    assert task.executor == "codex"


def test_disable_opencode_empties_the_menus(capsys, monkeypatch, tmp_path):
    _states(monkeypatch)
    assert cli.main(["providers", "disable", "opencode"]) == 0
    capsys.readouterr()
    store = Store()
    assert registry.menu(store, Role.EXECUTOR) == []
    assert registry.disabled_providers() == frozenset({"opencode"})
    # the refusal says which provider is off and how to turn it on
    with pytest.raises(registry.RegistryError, match="поставщик opencode выключен"):
        registry.pick(store, Role.EXECUTOR, None)
    project = make_project(tmp_path)
    with pytest.raises(tasks.TaskInvalid) as ei:
        tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="разведка"), project,
                     collect=False)
    assert "opencode" in " | ".join(ei.value.errors)
    # the provider row is off in the table
    assert cli.main(["providers"]) == 0
    row = next(r for r in _rows(capsys.readouterr().out) if r.startswith("opencode"))
    assert row.split()[3] == "выключен"
    assert cli.main(["providers", "enable", "opencode"]) == 0
    capsys.readouterr()
    assert [e.alias for e, d in registry.menu(store, Role.EXECUTOR) if d] == ["spark"]


def test_disable_keeps_the_rest_of_the_config(capsys, monkeypatch):
    _states(monkeypatch)
    write(paths.global_config_path(), '# мой хаб\nprojects = ["/srv/old"]\n\n[usage]\ngo_month_limit = 60.0\n')
    assert cli.main(["providers", "disable", "agy"]) == 0
    capsys.readouterr()
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "# мой хаб" in text and 'projects = ["/srv/old"]' in text
    assert "[usage]" in text and "go_month_limit = 60.0" in text
    assert "[providers.agy]" in text and "enabled = false" in text
    hub = config.load_hub()
    assert hub.tg_token == "" and hub.go_month_limit == 60.0 and hub.providers_off == ("agy",)


def test_json_of_enable_and_unknown_provider(capsys):
    assert cli.main(["--json", "providers", "enable", "opencode"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "name": "opencode", "enabled": True}
    assert cli.main(["providers", "disable", "no-such"]) == 2
    err = capsys.readouterr().err
    assert "no-such" in err and "opencode" in err


def test_the_switch_keeps_the_other_keys_of_the_section(capsys, monkeypatch):
    """The section is [providers.<name>]: another task adds `proxy` there — it must survive."""
    _states(monkeypatch)
    write(paths.global_config_path(), '[providers.codex]\nproxy = "http://127.0.0.1:8080"\n')
    assert cli.main(["providers", "disable", "codex"]) == 0
    capsys.readouterr()
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert text.count("[providers.codex]") == 1
    assert 'proxy = "http://127.0.0.1:8080"' in text and "enabled = false" in text
    assert config.load_hub().provider_enabled("codex") is False


def test_a_broken_switch_hides_nothing(capsys, monkeypatch):
    """A bad `enabled` value is a config error (doctor reports it); the registry must not hide every model."""
    _states(monkeypatch)
    write(paths.global_config_path(), '[providers.codex]\nenabled = "yes"\n')
    with pytest.raises(config.ConfigError):
        config.load_hub()
    assert cli.main(["providers"]) == 0
    assert "codex" in capsys.readouterr().out
    assert registry.disabled_providers() == frozenset()
    assert [e.alias for e, d in registry.menu(Store(), Role.EXECUTOR) if d] == ["spark"]


def test_switch_survives_a_repeat(capsys, monkeypatch):
    _states(monkeypatch)
    assert cli.main(["providers", "disable", "agy"]) == 0
    capsys.readouterr()
    assert cli.main(["providers", "disable", "agy"]) == 0
    capsys.readouterr()
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert text.count("[providers.agy]") == 1
    assert text.count("enabled = false") == 1


def test_hub_config_reads_the_switch(capsys, monkeypatch):
    _states(monkeypatch)
    write(paths.global_config_path(), '[providers.opencode]\nenabled = false\n[providers.agy]\nenabled = true\n')
    hub = config.load_hub()
    assert hub.providers_off == ("opencode",)
    assert hub.provider_enabled("agy") is True and hub.provider_enabled("opencode") is False
    assert registry.provider_enabled("agy") and not registry.provider_enabled("opencode")
    assert cli.main(["providers"]) == 0
    out = capsys.readouterr().out
    assert "выключен" in out and "включён" in out
