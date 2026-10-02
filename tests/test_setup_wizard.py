"""Мастер ahub setup: интерактив, --yes, бесплатный алиас, Telegram, запись конфига."""

from __future__ import annotations

import sys
from pathlib import Path

from ahub import cli, config, doctor, paths, registry
from ahub.model import Role
from ahub.store import Store
from tests.conftest import write
from tests.enginekit import make_repo


def _tty(monkeypatch, yes: bool = True):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: yes)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: yes)


def _answers(monkeypatch, items: list[str]):
    it = iter(items)

    def _fake(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("лишний input()")

    monkeypatch.setattr("builtins.input", _fake)


def _no_go(monkeypatch):
    monkeypatch.setattr(doctor, "auth_providers", lambda *a, **k: [])


def _fake_claude(monkeypatch, tmp_path: Path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: str(fake))
    return fake


def test_wizard_full_lang_project_free_telegram(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    _fake_claude(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "linux")
    root = tmp_path / "shop"
    make_repo(root)
    write(paths.global_config_path(), '# мой хаб\n[usage]\ngo_month_limit = 60.0\n')
    _answers(monkeypatch, [
        "",  # язык: умолчание ru
        str(root),  # путь проекта
        "",  # модели: да (умолчание)
        "n",  # служба install: нет
        "",  # служба start: нет (умолчание)
        "",  # claude: да (умолчание)
        "y",  # telegram: да
        "tok123",  # токен
        "77",  # chat id
    ])
    assert cli.main(["setup"]) == 0
    capsys.readouterr()
    hub = config.load_hub()
    assert hub.lang == "ru"
    assert str(root) in hub.projects
    assert (root / ".hub.toml").exists()
    assert config.load_project(root).name == "shop"
    # без входа в Go — роли на бесплатном алиасе
    assert doctor.check_models([]).ok is True
    free = doctor._free_alias(Store())
    for role in Role:
        menu = registry.menu(Store(), role)
        default = next((e for e, d in menu if d), None)
        assert default is not None and default.alias == free
    # Telegram записан, секции и комментарий целы
    assert hub.tg_token == "tok123" and hub.tg_chat_id == 77
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "# мой хаб" in text and "[usage]" in text and "go_month_limit = 60.0" in text
    assert "[telegram]" in text
    # навык и блок
    assert (Path.home() / ".claude/skills/ahub/SKILL.md").exists()
    assert "ahub:begin" in (root / "CLAUDE.md").read_text(encoding="utf-8")


def test_wizard_service_install_writes_units(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    from ahub.tg import launcher

    monkeypatch.setattr(launcher, "claude_bin", lambda: None)
    root = tmp_path / "proj"
    make_repo(root)
    _answers(monkeypatch, [
        "", str(root), "", "y",  # install: да
        "n",  # telegram: нет
    ])
    assert cli.main(["setup"]) == 0
    out = capsys.readouterr().out
    unit = Path.home() / ".config" / "systemd" / "user" / "ahub.service"
    assert unit.exists()
    assert "systemctl --user" in out


def test_yes_passes_without_input(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, True)
    _no_go(monkeypatch)

    def _boom(_prompt=""):
        raise AssertionError("input() при --yes")

    monkeypatch.setattr("builtins.input", _boom)
    root = tmp_path / "auto"
    make_repo(root)
    assert cli.main(["setup", str(root), "--yes"]) == 0
    capsys.readouterr()
    assert str(root) in config.load_hub().projects
    assert (root / ".hub.toml").exists()
    assert doctor.check_models([]).ok is True


def test_noninteractive_keeps_old_behavior_plus_free(tmp_path, monkeypatch, capsys):
    _tty(monkeypatch, False)
    _no_go(monkeypatch)
    root = tmp_path / "oldway"
    make_repo(root)
    assert cli.main(["setup", str(root), "--claude"]) == 0
    out = capsys.readouterr().out
    assert "шаблон v2" in out or "template" in out
    assert (root / ".hub.toml").exists()
    assert doctor.check_models([]).ok is True


def test_set_global_keeps_comments_and_sections(tmp_path, monkeypatch):
    from ahub.commands.setup import set_global

    monkeypatch.delenv("AHUB_TG_TOKEN", raising=False)
    monkeypatch.delenv("AHUB_TG_CHAT", raising=False)
    write(paths.global_config_path(), '# коммент\nprojects = ["/srv/old"]\n\n[usage]\ngo_month_limit = 10.0\n')
    set_global("lang", "ru")
    set_global("token", "t", section="telegram")
    set_global("chat_id", 5, section="telegram")
    text = paths.global_config_path().read_text(encoding="utf-8")
    assert "# коммент" in text and 'lang = "ru"' in text
    assert "[telegram]" in text and 'token = "t"' in text and "chat_id = 5" in text
    assert "[usage]" in text and "go_month_limit = 10.0" in text
    hub = config.load_hub()
    assert hub.lang == "ru" and hub.tg_token == "t" and hub.tg_chat_id == 5
