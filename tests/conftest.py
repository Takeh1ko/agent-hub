"""Shared fixtures: all state lives in a temp dir. The real ~/.local/share/opencode, ~/.local/share/ahub
and ~/.config are never touched (HOME is faked); live tests (real providers, real money) run only with AHUB_LIVE=1."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable
from typing import TypeVar

import pytest

_real_connect = sqlite3.connect


def _refusing_connect(database=":memory:", *args, **kwargs):
    """sqlite3.connect that refuses the live hub DB (anything else passes through).

    The suite fakes HOME/AHUB_HOME, so no test may open the real ~/.local/share/ahub (a test that
    resolves the DB path without the fakes fails here instead of writing live rows). The session
    guard below reads the live DB read-only (mode=ro URIs) — those stay allowed.
    """
    from ahub import paths as _paths

    if os.environ.get(_paths.UNDER_TEST) != "1":
        return _real_connect(database, *args, **kwargs)
    text = os.fspath(database) if isinstance(database, (str, os.PathLike)) else ""
    if kwargs.get("uri") and text.startswith("file:"):
        head, _, query = text[5:].partition("?")
        if "mode=ro" in query:
            return _real_connect(database, *args, **kwargs)
        if _paths.is_live_hub_path(head):
            raise RuntimeError(f"refusing live hub DB in tests: {database}")
        return _real_connect(database, *args, **kwargs)
    if _paths.is_live_hub_path(text):
        raise RuntimeError(f"refusing live hub DB in tests: {database}")
    return _real_connect(database, *args, **kwargs)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AHUB_HOME", str(tmp_path / "ahub-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("AHUB_LANG", "ru")
    monkeypatch.setenv("AHUB_PROBE", "0")  # no live model probes in tests (they would hit the network)
    monkeypatch.setenv("AHUB_UNDER_TEST", "1")  # children (worker, gates, service) refuse the live hub DB too
    for var in ("XDG_DATA_HOME", "XDG_STATE_HOME", "AHUB_FAKE_QUEUE", "AHUB_FAKE_PROVIDER", "LANG", "LC_ALL",
                "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sqlite3.connect", _refusing_connect)
    from ahub import log
    from ahub.i18n import _reset
    from ahub.prepare import PROVIDER_KEYS

    for name in PROVIDER_KEYS:  # a developer shell exports its provider keys; the doctor check must not read them
        monkeypatch.delenv(name, raising=False)
    _reset()  # language is picked lazily — reset it between tests
    from ahub import registry

    registry._cached_disabled = None  # the provider switch is cached by mtime — one test per file state
    log.setup()  # module loggers were built at import with the real HOME — send the log to the temp dir
    yield
    _reset()
    registry._cached_disabled = None


@pytest.fixture(scope="session", autouse=True)
def _real_db_untouched():
    """Suite guard: tests leave the live hub database and its dirs alone (T151).

    The live hub keeps working while the suite runs, so only test-shaped traces fail the run: rows
    of the fake project "P" (tasks and events — an event leak once slipped past a tasks-only check)
    and the accept lock of that project (no real project is named "P").
    """
    from ahub import paths as _paths

    real = _paths.live_home() / ".local/share/ahub/ahub.db"
    p_lock = _paths.live_home() / ".local/share/ahub/accept-P.lock"

    def counts():
        if not real.exists():
            return None
        con = _real_connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        try:
            return tuple(con.execute(f"SELECT COALESCE(MAX(id), 0) FROM {t}").fetchone()[0]
                          for t in ("task", "message", "question", "draft", "event"))
        except sqlite3.Error:
            return None
        finally:
            con.close()

    def leaked(table: str, after_id: int) -> int:
        """Test rows of the fake project 'P' written to the live hub while we ran (0 — cannot read it)."""
        if not real.exists():
            return 0
        con = _real_connect(f"file:{real}?mode=ro", uri=True, timeout=5)
        try:
            return con.execute(f"SELECT COUNT(*) FROM {table} WHERE project='P' AND id > ?",
                               (after_id,)).fetchone()[0]
        except sqlite3.Error:  # a locked live database is doctor/speak, not a failed guard
            return 0
        finally:
            con.close()

    def lock_stat():
        try:
            st = p_lock.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    before = counts()
    task_max, event_max = (before[0], before[4]) if before is not None else (0, 0)
    lock_before = lock_stat()
    yield
    after = counts()
    if before is not None and after is not None:
        # the live hub may have added rows of its own while we ran — tests only write to tmp; make sure
        # no test row leaked out (fake project "P")
        assert leaked("task", task_max) == 0, "тесты записали задачи в боевую базу хаба"
        assert leaked("event", event_max) == 0, "тесты записали события в боевую базу хаба"
    lock_after = lock_stat()
    if lock_before is None:
        assert lock_after is None, "тесты создали accept-P.lock в живом каталоге хаба"
    elif lock_after is not None:
        # removed meanwhile (cleanup) is fine; created-or-touched by tests is not
        assert lock_after == lock_before, "тесты трогали accept-P.lock в живом каталоге хаба"


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
