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
    # Боевую базу параллельно пишет живой бот (события, meta, автоимпорт) — mtime не показатель.
    # Утечка тестов = новые строки в таблицах, куда пишут только владелец/тесты.
    guarded = ("tg_chat", "inbox", "question", "outbox")

    def rows() -> dict:
        if not combat.exists():
            return {}
        import sqlite3

        con = sqlite3.connect(f"file:{combat}?mode=ro", uri=True, timeout=5)
        try:
            out = {}
            for t in guarded:
                try:
                    out[t] = {tuple(r) for r in con.execute(f"SELECT * FROM {t}")}
                except sqlite3.Error:
                    out[t] = set()
            return out
        finally:
            con.close()

    before_present = combat.exists()
    before = rows()
    yield
    if not before_present:
        assert not combat.exists(), "тесты создали боевой hub.db"
        return
    after = rows()
    for t in guarded:
        # Владелец мог написать боту во время прогона — это inbox с source='tg' и живым текстом;
        # тестовые строки узнаются по отсутствию в «до» и по признакам фикстур.
        new = after.get(t, set()) - before.get(t, set())
        leaked = [r for r in new if t != "inbox" or not str(r).count("'tg'")]
        assert not leaked, f"тесты записали в боевой hub.db ({t}): {leaked[:3]}"


@pytest.fixture(autouse=True)
def _reset_pulse_cooldown():
    """Кулдаун пульс-событий бота — состояние модуля; между тестами не переносим."""
    try:
        from hub.bot import core as _core

        _core._LAST_PULSE.clear()
    except Exception:
        pass
    yield
