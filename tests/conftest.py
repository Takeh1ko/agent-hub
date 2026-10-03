"""Shared fixtures: all state lives in a temp dir. The real ~/.local/share/opencode, ~/.local/share/ahub
and ~/.config are never touched (HOME is faked); live tests (real providers, real money) run only with AHUB_LIVE=1."""

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
    monkeypatch.setenv("AHUB_LANG", "ru")
    monkeypatch.setenv("AHUB_PROBE", "0")  # no live model probes in tests (they would hit the network)
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "AHUB_FAKE_QUEUE", "AHUB_FAKE_PROVIDER", "LANG", "LC_ALL",
                "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    from ahub import log
    from ahub.i18n import _reset

    _reset()  # language is picked lazily — reset it between tests
    log.setup()  # module loggers were built at import with the real HOME — send the log to the temp dir
    yield
    _reset()


@pytest.fixture(scope="session", autouse=True)
def _real_db_untouched():
    """Suite guard: tests left the live hub database alone (tasks and messages)."""
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

    def count_p():
        if not real.exists():
            return 0
        import sqlite3

        con = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute("SELECT COUNT(*) FROM task WHERE project='P'").fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            con.close()

    p_before = count_p()
    before = counts()
    yield
    after = counts()
    if before is not None and after is not None:
        # the live hub may have added rows of its own while we ran — tests only write to tmp; make sure
        # no test row leaked out (fake project "P")
        p_after = count_p()
        assert p_after == p_before, "тесты записали задачи в боевую базу хаба"


def write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_runner_stop():
    """The runner's process-wide stop flag (set by request_stop) must not leak into the next test."""
    from ahub.providers import runner

    runner.reset_stop()
    yield
    runner.reset_stop()


@pytest.fixture
def own_signals():
    """Signal handlers of the test process: a test that installs the worker's handlers puts them back."""
    import signal

    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, handler in old.items():
        signal.signal(s, handler)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("AHUB_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="живой тест: AHUB_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
