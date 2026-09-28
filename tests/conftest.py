"""Общие фикстуры. Тесты не трогают настоящие ~/.local/share/opencode и ~/.local/share/agent-hub:
HOME подменяется на временный каталог."""

import pytest


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENT_HUB_HOME", str(tmp_path / ".local/share/agent-hub"))
    yield tmp_path
