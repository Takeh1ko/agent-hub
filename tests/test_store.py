from __future__ import annotations

import sqlite3

import pytest

from ahub import paths
from ahub.model import TRANSITIONS, Ev, Kind, State, can_move, parse_task_id
from ahub.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


def test_default_path_and_migration(store):
    assert store.path == paths.db_path()
    assert store.schema_version() >= 1
    Store()  # a second open neither fails nor applies the migrations again
    with store.read() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"task", "task_dep", "session", "event", "question", "message", "draft", "model",
            "role_model", "presence", "claude_launch", "observer_report", "tg_chat", "op", "meta"} <= tables


def test_meta(store):
    assert store.meta_get("x") is None
    store.meta_set("x", "1")
    store.meta_set("x", "2")
    assert store.meta_get("x") == "2"
    store.meta_del("x")
    assert store.meta_get("x", "d") == "d"


def test_create_and_get_task(store):
    a = store.create_task(project="P", kind=Kind.CODE, title="сделать", spec="подробно",
                          executor="spark", review={"models": ["spark"], "rounds": 2},
                          limits={"allowed_paths": ["core/**"]}, budget_go=1.5, created_by="orchestrator",
                          now=1000)
    b = store.create_task(project="P", kind="scout", title="узнать", after=[a], now=2000)
    ta = store.get_task(a)
    assert ta.label == f"T{a}"
    assert ta.kind is Kind.CODE and ta.state is State.QUEUED
    assert ta.review == {"models": ["spark"], "rounds": 2}
    assert ta.limits == {"allowed_paths": ["core/**"]}
    assert ta.created_at == ta.updated_at == 1000
    assert store.get_task(b).after == [a]
    assert store.dependents_of(a) == [b]
    evs = store.events()
    assert [e.kind for e in evs] == ["created", "created"]
    assert evs[0].task_id == a and evs[0].payload["by"] == "orchestrator"
    assert store.get_task(999) is None


def test_create_only_queued_or_draft(store):
    with pytest.raises(ValueError):
        store.create_task(project="P", kind="scout", title="x", state=State.WORKING)
    tid = store.create_task(project="P", kind="scout", title="x", state=State.DRAFT)
    assert store.get_task(tid).state is State.DRAFT


def test_create_rolls_back_on_bad_dependency(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.create_task(project="P", kind="scout", title="x", after=[12345])
    assert store.list_tasks() == []
    assert store.events() == []


def test_list_tasks_filters(store):
    ids = [store.create_task(project=p, kind="scout", title=p) for p in ("A", "B", "A")]
    assert [t.id for t in store.list_tasks(project="A")] == [ids[0], ids[2]]
    assert [t.id for t in store.list_tasks(newest_first=True, limit=2)] == [ids[2], ids[1]]
    assert store.list_tasks(states=set()) == []
    assert len(store.list_tasks(states={State.QUEUED})) == 3


def test_update_task_plain_only(store):
    tid = store.create_task(project="P", kind="code", title="x")
    store.update_task(tid, branch="ahub/T1", limits={"allowed_paths": ["a"]}, now=5)
    t = store.get_task(tid)
    assert t.branch == "ahub/T1" and t.limits == {"allowed_paths": ["a"]} and t.updated_at == 5
    for bad in ("state", "owner", "version", "lease_until"):
        with pytest.raises(ValueError):
            store.update_task(tid, **{bad: "x"})


def test_events_reaction_and_filters(store):
    tid = store.create_task(project="P", kind="scout", title="x")
    e1 = store.add_event(Ev.PHASE, task_id=tid, payload={"phase": "studying"})
    e2 = store.add_event(Ev.DONE, task_id=tid, project="P")
    e3 = store.add_event(Ev.ALARM, critical=True, payload={"text": "поставщик лёг"})
    got = store.events(needs_reaction=True)
    assert [e.id for e in got] == [e2, e3]
    assert got[1].critical and got[1].task_id is None
    assert [e.id for e in store.events(after_id=e1, task_id=tid)] == [e2]
    assert store.last_event_id() == e3
    with pytest.raises(ValueError):
        store.add_event("нет-такого")


def test_sessions(store):
    tid = store.create_task(project="P", kind="scout", title="x")
    sid = store.add_session(task_id=tid, provider="opencode", role="scout", model="spark", now=7)
    store.update_session(sid, external_id="ses_1", status="ok", cost_go=0.12, tokens={"input": 10})
    s = store.get_session(sid)
    assert s.external_id == "ses_1" and s.cost_go == 0.12 and s.tokens == {"input": 10} and s.started_at == 7
    assert [x.id for x in store.list_sessions(tid, status="ok")] == [sid]
    other = store.add_session(task_id=tid, provider="opencode", role="reviewer")
    with pytest.raises(sqlite3.IntegrityError):  # one external_id per provider — one session
        store.update_session(other, external_id="ses_1")
    with pytest.raises(ValueError):
        store.update_session(sid, task_id=5)


def test_tx_rolls_back(store):
    with pytest.raises(RuntimeError):
        with store.tx() as c:
            store.create_task(project="P", kind="scout", title="x", con=c)
            raise RuntimeError("стоп")
    assert store.list_tasks() == []


def test_tx_retries_locked_until_released(store, monkeypatch):
    """A held write lock → BEGIN retries with back-off and succeeds once released."""
    import threading
    import time

    import ahub.store as store_mod

    monkeypatch.setattr(store_mod, "LOCK_RETRY_BUDGET_S", 5.0)
    monkeypatch.setattr(store_mod, "LOCK_RETRY_BASE_S", 0.01)
    monkeypatch.setattr(store_mod, "LOCK_RETRY_CAP_S", 0.05)
    monkeypatch.setattr(store_mod, "BUSY_TIMEOUT_MS", 50)
    holder = sqlite3.connect(str(store.path), timeout=5, isolation_level=None,
                             check_same_thread=False)
    holder.execute("PRAGMA busy_timeout=50")
    try:
        holder.execute("BEGIN IMMEDIATE")

        def _release():
            time.sleep(0.3)
            try:
                holder.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            finally:
                try:
                    holder.close()
                except sqlite3.Error:
                    pass

        th = threading.Thread(target=_release)
        th.start()
        store.meta_set("k", "v")  # first BEGIN times out, retry wins after release
        th.join(10)
        assert store.meta_get("k") == "v"
    finally:
        try:
            holder.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        try:
            holder.close()
        except sqlite3.Error:
            pass


def test_tx_does_not_retry_other_errors(store, monkeypatch):
    """A non-lock OperationalError raises at once — it is never retried."""

    calls: list[str] = []

    class _FakeCon:
        def execute(self, sql, *args, **kw):
            if isinstance(sql, str) and "BEGIN" in sql:
                calls.append(sql)
                raise sqlite3.OperationalError("no such table: foo")
            raise AssertionError(f"unexpected {sql!r}")

        def close(self) -> None:
            pass

    fake = _FakeCon()
    monkeypatch.setattr(store, "_open", lambda: fake)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        with store.tx():
            pass
    assert len(calls) == 1


def test_is_lock_error_only_for_lock():
    from ahub.store import is_lock_error

    assert is_lock_error(sqlite3.OperationalError("database is locked"))
    assert is_lock_error(sqlite3.OperationalError("database is busy"))
    assert not is_lock_error(sqlite3.OperationalError("no such table: foo"))
    assert not is_lock_error(sqlite3.IntegrityError("UNIQUE failed"))
    assert not is_lock_error(ValueError("database is locked"))


def test_rows_tolerate_columns_of_a_newer_schema(store):
    """A migration adds a column under a live process — the old code still reads every row type."""
    tid = store.create_task(project="P", kind=Kind.CODE, title="x", now=1000)
    sid = store.add_session(task_id=tid, provider="fake", role="executor", now=1000)
    store.update_session(sid, external_id="ses_1", tokens={"input": 10})
    eid = store.add_event("created", task_id=tid, project="P", payload={"k": 1}, now=1000)
    with store.tx() as c:  # what a newer code does to the shared database
        for table in ("task", "session", "event"):
            c.execute(f"ALTER TABLE {table} ADD COLUMN note TEXT NOT NULL DEFAULT ''")

    t = store.get_task(tid)
    assert t.id == tid and t.kind is Kind.CODE and t.state is State.QUEUED and t.request_text == ""
    assert [x.id for x in store.list_tasks()] == [tid]
    s = store.get_session(sid)
    assert s.id == sid and s.external_id == "ses_1" and s.tokens == {"input": 10}
    assert [x.id for x in store.list_sessions(tid)] == [sid]
    assert [e.payload for e in store.events() if e.id == eid] == [{"k": 1}]


def test_transition_table_consistent():
    for src, dsts in TRANSITIONS.items():
        assert src not in dsts, f"петля {src}"
    assert not TRANSITIONS[State.ACCEPTED] and not TRANSITIONS[State.REJECTED]
    assert can_move("done", "accepting") and not can_move("queued", "done")
    # From any state except the finals and the draft there is a path to a final one.
    for s in State:
        seen, stack = set(), [s]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(TRANSITIONS[cur])
        assert State.ACCEPTED in seen or State.REJECTED in seen or s in (State.ACCEPTED,)


def test_parse_task_id():
    assert parse_task_id("T12") == parse_task_id("t12") == parse_task_id("12") == parse_task_id(12) == 12
    with pytest.raises(ValueError):
        parse_task_id("X1")


def test_list_tasks_projects_filter_and_task_projects(store):
    """The project filter of the task list and the projects it knows — the owner's scope view."""
    store.create_task(project="alpha", kind=Kind.CODE, title="a1")
    store.create_task(project="beta", kind=Kind.CODE, title="b1")
    store.create_task(project="gamma", kind=Kind.CODE, title="g1")

    assert store.task_projects() == ["alpha", "beta", "gamma"]

    tasks_ab = store.list_tasks(projects=("alpha", "beta"))
    assert sorted(t.project for t in tasks_ab) == ["alpha", "beta"]

    tasks_g = store.list_tasks(projects=("gamma",))
    assert [t.project for t in tasks_g] == ["gamma"]

    tasks_none = store.list_tasks(projects=("delta",))
    assert tasks_none == []
