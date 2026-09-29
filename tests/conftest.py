"""Общие фикстуры. Тесты не трогают настоящие ~/.local/share/opencode и ~/.local/share/agent-hub:
HOME подменяется на временный каталог."""

import os
import pwd
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path / ".local/share/agent-hub"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    yield tmp_path


@pytest.fixture(scope="session", autouse=True)
def _no_combat_write():
    """Страж набора: боевой hub.db не создан/не изменён за весь прогон."""
    try:
        real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        yield
        return
    combat = real_home / ".local/share/agent-hub/hub.db"
    before_present = combat.exists()
    before_mtime = combat.stat().st_mtime if before_present else None
    before_size = combat.stat().st_size if before_present else None
    yield
    after_present = combat.exists()
    if not before_present:
        assert not after_present, "тесты создали боевой hub.db"
        return
    assert after_present, "тесты удалили боевой hub.db"
    assert combat.stat().st_mtime == before_mtime, "тесты изменили боевой hub.db"
    assert combat.stat().st_size == before_size
