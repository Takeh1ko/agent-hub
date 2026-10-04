"""Shared fixtures: all state lives in a temp dir. The real ~/.local/share/opencode, ~/.local/share/ahub
and ~/.config are never touched (HOME is faked); live tests (real providers, real money) run only with AHUB_LIVE=1."""

from __future__ import annotations

import os
import pwd
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

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
    from ahub import registry

    registry._cached_disabled = None  # the provider switch is cached by mtime — one test per file state
    log.setup()  # module loggers were built at import with the real HOME — send the log to the temp dir
    yield
    _reset()
    registry._cached_disabled = None


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

    def leaked_p(after_id: int) -> int:
        """Test rows of the fake project 'P' written to the live hub while we ran (0 — cannot read it)."""
        if not real.exists():
            return 0
        import sqlite3

        con = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute("SELECT COUNT(*) FROM task WHERE project='P' AND id > ?",
                               (after_id,)).fetchone()[0]
        except sqlite3.Error:  # a locked live database is doctor/speak, not a failed guard
            return 0
        finally:
            con.close()

    before = counts()
    yield
    after = counts()
    if before is not None and after is not None:
        # the live hub may have added rows of its own while we ran — tests only write to tmp; make sure
        # no test row leaked out (fake project "P")
        assert leaked_p(before[0]) == 0, "тесты записали задачи в боевую базу хаба"


WAIT_S = 60.0  # a deadline for what a thread or a process of its own is about to do: under load no pause is a promise
_T = TypeVar("_T")


def wait_until(cond: Callable[[], _T], timeout: float = WAIT_S, step: float = 0.05) -> _T | None:
    """Poll `cond()` until it gives something true and return that value; None when `timeout` is over.

    A side effect of another thread or process happens when the machine says so, so a test waits for the
    condition and asserts on what it waited for (None — the wait is over and the assert of the caller says so).
    """
    end = time.monotonic() + timeout
    while True:
        got = cond()
        if got:
            return got
        if time.monotonic() >= end:
            return None
        time.sleep(step)


def write(path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


async def rows_ready(app, pilot, want: int | list[int] = 1) -> None:
    """`ahub top` builds the rows of a refresh in a worker thread — wait for them, do not guess a pause.

    `want` — a count (wait until the table has at least that many rows) or the exact list of row ids
    (wait until the table shows exactly them: the refresh of the `o` filter can have the same count as
    the one before it, and under load 0.4 s is not a promise).
    """
    for _ in range(100):
        ids = list(app._ids)
        if (ids == want) if isinstance(want, list) else (len(ids) >= want):
            return
        await pilot.pause(0.1)
    raise AssertionError(f"the table of `ahub top` shows {list(app._ids)}, not {want!r}")


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
