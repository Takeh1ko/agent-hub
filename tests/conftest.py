"""Shared fixtures: all state lives in a temp dir. The real ~/.local/share/opencode, ~/.local/share/ahub
and ~/.config are never touched (HOME is faked); live tests (real providers, real money) run only with AHUB_LIVE=1."""

from __future__ import annotations

import os
import sqlite3
import time
import warnings
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
    from ahub import catalog, registry

    registry._cached_disabled = None  # the provider switch is cached by mtime — one test per file state
    catalog.reset_cache()  # provider catalogs are cached in memory — one test per fetch
    log.setup()  # module loggers were built at import with the real HOME — send the log to the temp dir
    yield
    _reset()
    registry._cached_disabled = None
    catalog.reset_cache()


@pytest.fixture(scope="session", autouse=True)
def _real_db_untouched():
    """Session backstop: report test-shaped traces that reached the live hub (T151).

    Live opens from this suite fail loudly at open time instead — the sqlite refusal above fires in
    this process, and Store refuses live paths in every child through the inherited AHUB_UNDER_TEST
    flag — so rows found here are never silently ours. The live DB is shared by concurrent suites,
    and failing this run for another suite's rows blamed T144/T129 before: report with content
    (table, ids, titles) for manual attribution, do not fail.
    """
    from ahub import paths as _paths

    real = _paths.live_home() / ".local/share/ahub/ahub.db"
    p_lock = _paths.live_home() / ".local/share/ahub/accept-P.lock"

    before = _counts(real)
    task_max, event_max = (before[0], before[4]) if before is not None else (0, 0)
    existed = real.exists()
    lock_before = _lock_stat(p_lock)
    yield
    for line in _live_traces(real, p_lock, before, task_max, event_max, existed, lock_before):
        warnings.warn(f"live hub gained test-shaped traces during this run: {line} "
                      f"(shared dev DB — this suite refuses live opens, see refusal errors above if any)",
                      stacklevel=2)


def _lock_stat(path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _live_traces(real, p_lock, before, task_max, event_max, existed, lock_before) -> list[str]:
    """New test-shaped rows/locks in the live hub since session start (empty — none)."""
    out: list[str] = []
    after = _counts(real) if real.exists() else None
    if after is not None and before is not None:
        for table, base, col in (("task", task_max, "title"), ("event", event_max, "kind")):
            rows = _leaked_rows(real, table, base, col)
            if rows:
                shown = ", ".join(f"#{i} {v}" for i, v in rows[:3])
                more = f" +{len(rows) - 3} more" if len(rows) > 3 else ""
                out.append(f"{table}: {len(rows)} new 'P' rows ({shown}{more})")
    elif not existed and real.exists():
        out.append("live hub DB appeared mid-run")
    lock_after = _lock_stat(p_lock)
    if lock_before is None and lock_after is not None:
        out.append("accept-P.lock created in the live hub dir")
    elif lock_before is not None and lock_after is not None and lock_after != lock_before:
        out.append("accept-P.lock touched in the live hub dir")
    return out


def _counts(real):
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


def _leaked_rows(real, table: str, after_id: int, col: str) -> list[tuple[int, str]]:
    """Test rows of the fake project 'P' (id, title/kind) — [] when the live DB is unreadable."""
    if not real.exists():
        return []
    con = _real_connect(f"file:{real}?mode=ro", uri=True, timeout=5)
    try:
        return [(r[0], str(r[1])[:60]) for r in
                con.execute(f"SELECT id, {col} FROM {table} WHERE project='P' AND id > ? ORDER BY id",
                            (after_id,))]
    except sqlite3.Error:  # a locked live database is doctor/speak, not a failed guard
        return []
    finally:
        con.close()


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


async def wait_for(pilot, cond: Callable[[], bool], timeout: float = 15.0) -> None:
    """Wait for `cond()` in the pilot loop — a worker thread lands when it lands.

    A fixed pause is not a wait under load: poll the state the test is about to assert.
    On timeout just return and let the caller's assert say so (assertions stay as strict).
    """
    end = time.monotonic() + timeout
    while True:
        if cond():
            return
        if time.monotonic() >= end:
            return
        await pilot.pause(0.05)


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


_REAL_PROVIDERS = frozenset({"opencode", "agy", "codex"})
_REAL_BINARIES = frozenset({"opencode", "agy", "codex"})


def _real_binary_provider(cmd) -> str:
    """Provider of a real worker-session command ('' — not a worker run).

    Only worker turns are refused here (opencode run, agy stream-json, codex exec);
    catalog/health/quota probes (models, --version, /usage) stay allowed so the
    existing visibility tests keep working without the network. A fake stub under
    /tmp (the provider contract tests) is not a real binary either.
    """
    try:
        parts = list(cmd) if cmd else []
    except TypeError:
        return ""
    if not parts:
        return ""
    import os as _os
    import tempfile as _tf

    first = str(parts[0])
    base = _os.path.basename(first)
    if base not in _REAL_BINARIES:
        return ""
    if first.startswith(_tf.gettempdir() + "/") or "/pytest-" in first:
        return ""
    text = " ".join(str(p) for p in parts)
    if base == "opencode" and "run" in parts:
        return base
    if base == "agy" and "-p" in parts and "stream-json" in text:
        return base
    if base == "codex" and "exec" in parts:
        return base
    return ""


@pytest.fixture(autouse=True)
def _no_real_provider_binaries(monkeypatch):
    """Tests never run a real provider: launching opencode/agy/codex fails at once.

    While AHUB_UNDER_TEST=1 and not AHUB_LIVE=1, a run through the runner or a
    direct subprocess launch of a real provider binary raises with the alias and
    the provider named, instead of spending ~25 s on the network and flaking.
    """
    from ahub.providers import runner as _runner

    _real_run = _runner.run

    def _guarded_run(provider, spec, **kw):
        import os as _os
        import tempfile as _tf

        pname = getattr(provider, "name", "") or ""
        if _os.environ.get("AHUB_UNDER_TEST") == "1" and _os.environ.get("AHUB_LIVE") != "1":
            if pname in _REAL_PROVIDERS:
                binary = ""
                try:
                    if hasattr(provider, "_bin"):
                        binary = str(provider._bin())
                    else:
                        binary = str(getattr(provider, "binary", "") or "")
                except Exception:
                    binary = ""
                tmp = _tf.gettempdir()
                # contract tests run the real provider class on a fake stub under /tmp —
                # that is not the network, only a system binary (or the default) is refused
                is_fake_stub = bool(binary) and (binary.startswith(tmp + "/") or "/pytest-" in binary
                                                 or binary in ("/bin/echo", "/bin/true"))
                if not is_fake_stub:
                    raise AssertionError(
                        f"refusing real provider '{pname}' (alias model '{spec.model_id}') in tests: "
                        f"point the alias at the fake provider (tests/enginekit.ensure_fake_model) "
                        f"or set AHUB_LIVE=1")
        return _real_run(provider, spec, **kw)

    monkeypatch.setattr(_runner, "run", _guarded_run)

    import subprocess as _sp

    _real_popen = _sp.Popen
    _real_run_sub = _sp.run

    def _guarded_popen(cmd, *a, **kw):
        import os as _os

        if _os.environ.get("AHUB_UNDER_TEST") == "1" and _os.environ.get("AHUB_LIVE") != "1":
            prov = _real_binary_provider(cmd) if isinstance(cmd, (list, tuple)) else ""
            if prov:
                raise AssertionError(
                    f"refusing real provider binary '{cmd[0]}' (provider '{prov}') in tests: "
                    f"use the fake provider or set AHUB_LIVE=1")
        return _real_popen(cmd, *a, **kw)

    def _guarded_sub_run(cmd, *a, **kw):
        import os as _os

        if _os.environ.get("AHUB_UNDER_TEST") == "1" and _os.environ.get("AHUB_LIVE") != "1":
            prov = _real_binary_provider(cmd) if isinstance(cmd, (list, tuple)) else ""
            if prov:
                raise AssertionError(
                    f"refusing real provider binary '{cmd[0]}' (provider '{prov}') in tests: "
                    f"use the fake provider or set AHUB_LIVE=1")
        return _real_run_sub(cmd, *a, **kw)

    monkeypatch.setattr(_sp, "Popen", _guarded_popen)
    monkeypatch.setattr(_sp, "run", _guarded_sub_run)
    yield


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
