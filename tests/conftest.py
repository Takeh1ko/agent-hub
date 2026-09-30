"""Общие фикстуры: всё состояние — во временном каталоге. Настоящие ~/.local/share/opencode, ~/.local/share/ahub
и ~/.config не трогаются (HOME подменяется); живые тесты (настоящие поставщики, деньги) — только при AHUB_LIVE=1."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AHUB_HOME", str(tmp_path / "ahub-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "AHUB_FAKE_QUEUE"):
        monkeypatch.delenv(var, raising=False)
    from ahub import log

    log.setup()  # логгеры модулей созданы при импорте с настоящим HOME — перенаправить лог во временный каталог
    yield


@pytest.fixture(scope="session", autouse=True)
def _real_db_untouched():
    """Страж набора: боевая база хаба не изменена тестами (задачи и сообщения)."""
    try:
        real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".local/share/ahub/ahub.db"
    except (KeyError, OSError):
        yield
        return

    def counts():
        if not real.exists():
            return None
        import sqlite3

        con = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        try:
            return tuple(con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]
                         for t in ("task", "message", "question", "draft"))
        except sqlite3.Error:
            return None
        finally:
            con.close()

    before = counts()
    yield
    after = counts()
    if before is not None and after is not None:
        # живой хаб мог добавить свои строки во время прогона — тесты пишут только в tmp; сверяем, что
        # тестовые заголовки не просочились (фиктивный проект «P»)
        import sqlite3

        con = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        try:
            leaked = con.execute("SELECT COUNT(*) FROM task WHERE project='P'").fetchone()[0]
        finally:
            con.close()
        assert leaked == 0, "тесты записали задачи в боевую базу хаба"


def write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def pytest_collection_modifyitems(config, items):
    if os.environ.get("AHUB_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="живой тест: AHUB_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
