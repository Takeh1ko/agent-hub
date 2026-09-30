"""Фикстуры v2: всё состояние хаба — во временном каталоге (корневой conftest уже подменил HOME)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ahub_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AHUB_HOME", str(tmp_path / "ahub-home"))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    yield


def write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def pytest_collection_modifyitems(config, items):
    """Живые тесты (настоящие поставщики, деньги) — только при AHUB_LIVE=1."""
    import os

    if os.environ.get("AHUB_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="живой тест: AHUB_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
