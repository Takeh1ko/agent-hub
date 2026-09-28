"""wait: next_events, ожидание, таймаут, CLI."""

from __future__ import annotations

import sqlite3
import threading
import time

import hub.read.events as ev
from hub.cli import main
from hub.store import Store


def _seed() -> Store:
    s = Store()
    s.upsert_task(id="T1", stage="exec r1")
    s.upsert_task(id="T2", stage="exec r1")
    return s


def test_next_events_after_and_task():
    s = _seed()
    e1 = s.add_event("T1", "stage", {"a": 1})
    e2 = s.add_event("T2", "stuck", {})
    e3 = s.add_event("T1", "answer", {})
    assert [e["id"] for e in ev.next_events(s, 0)] == [e1, e2, e3]
    assert [e["id"] for e in ev.next_events(s, e1)] == [e2, e3]
    assert [e["id"] for e in ev.next_events(s, 0, "T1")] == [e1, e3]
    assert ev.next_events(s, e3) == []
    assert ev.next_events(s, 0, "нет") == []


def test_wait_immediate():
    s = _seed()
    e1 = s.add_event("T1", "stage", {})
    got = ev.wait(s, 0, timeout_s=5, poll_s=0.05)
    assert [e["id"] for e in got] == [e1]


def test_wait_background_event():
    s = _seed()
    known = s.events_since(0)
    after = max((r["id"] for r in known), default=0)

    def _later():
        time.sleep(0.05)
        s.add_event("T1", "owner_message", {"t": "привет"})

    th = threading.Thread(target=_later)
    th.start()
    got = ev.wait(s, after, timeout_s=5, poll_s=0.05)
    th.join()
    assert len(got) == 1 and got[0]["kind"] == "owner_message"


def test_wait_timeout_fake_clock():
    s = _seed()
    after = max((r["id"] for r in s.events_since(0)), default=0)
    calls = [0.0, 1000.0]
    n_calls = 0

    def _clock():
        nonlocal n_calls
        n_calls += 1
        return calls.pop(0) if len(calls) > 1 else calls[0]

    t0 = time.monotonic()
    assert ev.wait(s, after, timeout_s=10, poll_s=0.05, clock=_clock) == []
    # Часы правда используются (дедлайн и проверки), а не игнорируются.
    assert n_calls >= 2
    assert time.monotonic() - t0 < 2


def test_wait_timeout_real_short():
    s = _seed()
    after = max((r["id"] for r in s.events_since(0)), default=0)
    t0 = time.monotonic()
    assert ev.wait(s, after, timeout_s=0.1, poll_s=0.05) == []
    assert time.monotonic() - t0 < 5


def test_wait_writes_listen_ts():
    s = _seed()
    ev.wait(s, 0, timeout_s=0.1, poll_s=0.05)
    con = sqlite3.connect(str(s.path))
    try:
        row = con.execute(
            "SELECT value FROM meta WHERE key='claude_listen_ts'").fetchone()
    finally:
        con.close()
    assert row is not None and int(row[0]) > 0


def test_wait_touch_each_poll(monkeypatch):
    """Пульс пишется при каждом опросе, а не раз за вызов."""
    s = _seed()
    after = max((r["id"] for r in s.events_since(0)), default=0)
    n = 0
    real = ev._touch_listen

    def _counting(store):
        nonlocal n
        n += 1
        return real(store)

    monkeypatch.setattr(ev, "_touch_listen", _counting)
    assert ev.wait(s, after, timeout_s=0.2, poll_s=0.05) == []
    assert n >= 2


def test_wait_touch_failure_keeps_waiting(monkeypatch):
    """Ошибка пульса не роняет ожидание (best-effort)."""
    s = _seed()
    e1 = s.add_event("T1", "stage", {})

    def _boom(store):
        raise sqlite3.Error("диск занят")

    monkeypatch.setattr(ev, "_touch_listen", _boom)
    got = ev.wait(s, 0, timeout_s=5, poll_s=0.05)
    assert [e["id"] for e in got] == [e1]


def test_cli_timeout_1m_empty(monkeypatch, capsys):
    Store()  # пустой store

    seen: dict = {}

    def _fake(store, after_id, task_id=None, timeout_s=0, poll_s=0.5, clock=None):
        seen["timeout_s"] = timeout_s
        seen["task"] = task_id
        seen["poll_s"] = poll_s
        return []

    monkeypatch.setattr(ev, "wait", _fake)
    assert main(["wait", "--timeout", "1м"]) == 2
    assert capsys.readouterr().out == ""
    assert abs(seen["timeout_s"] - 60.0) < 0.001
    assert seen["task"] is None
    assert abs(seen["poll_s"] - 0.5) < 1e-9
    # Опции пробрасываются.
    assert main(["wait", "--timeout", "1м", "--task", "T1",
                 "--poll", "0.05"]) == 2
    assert seen["task"] == "T1"
    assert abs(seen["poll_s"] - 0.05) < 1e-9


def test_cli_real_short_timeout_empty(capsys):
    Store()  # пусто
    t0 = time.monotonic()
    assert main(["wait", "--timeout", "1с", "--poll", "0.05"]) == 2
    assert capsys.readouterr().out == ""
    assert 0.5 < time.monotonic() - t0 < 5


def test_cli_old_events_not_replayed(capsys):
    s = _seed()
    s.add_event("T1", "stage", {"старое": 1})
    assert main(["wait", "--timeout", "1с", "--poll", "0.05"]) == 2
    assert capsys.readouterr().out == ""


def test_cli_prints_delta(monkeypatch, capsys):
    s = _seed()
    ready = threading.Event()
    orig_since = Store.events_since

    def _wrapped(self, eid):
        res = orig_since(self, eid)
        ready.set()
        return res

    monkeypatch.setattr(Store, "events_since", _wrapped)

    def _later():
        assert ready.wait(timeout=5)
        time.sleep(0.02)
        s.add_event("T1", "answer", {"ok": True})

    th = threading.Thread(target=_later)
    th.start()
    assert main(["wait", "--timeout", "5с", "--poll", "0.05"]) == 0
    th.join()
    out = capsys.readouterr().out.strip()
    assert "answer" in out and "T1" in out


def test_cli_bad_timeout(capsys):
    assert main(["wait", "--timeout", "херня"]) == 2
    assert "непонятный --timeout" in capsys.readouterr().err
