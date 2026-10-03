from __future__ import annotations

import multiprocessing as mp

import pytest

from ahub import transitions as tr
from ahub.model import Ev, State
from ahub.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


def _task(store, **kw) -> int:
    return store.create_task(project="P", kind=kw.pop("kind", "code"), title="x", **kw)


def test_move_writes_state_and_events(store):
    tid = _task(store)
    t = tr.move(store, tid, State.PREPARING, reason="старт", by="service", now=10,
                fields={"branch": "ahub/T1", "round": 1})
    assert t.state is State.PREPARING and t.branch == "ahub/T1" and t.round == 1 and t.version == 1
    ev = store.events(task_id=tid)[-1]
    assert ev.kind == "state" and ev.payload == {"from": "queued", "to": "preparing", "reason": "старт",
                                                 "by": "service"}


def test_move_to_done_emits_reaction_event_and_releases(store):
    tid = _task(store, kind="scout")
    tr.move(store, tid, State.PREPARING)
    assert tr.acquire(store, tid, "own", pid=1, now=100)
    tr.move(store, tid, State.WORKING, owner="own", now=110)
    t = tr.move(store, tid, State.DONE, owner="own", reason="отчёт готов", payload={"summary": "3 строки"},
                now=120)
    assert t.owner == "" and t.lease_until is None and t.finished_at is None
    kinds = [e.kind for e in store.events(task_id=tid, needs_reaction=True)]
    assert kinds == ["done"]
    assert store.events(task_id=tid, needs_reaction=True)[0].payload == {"reason": "отчёт готов",
                                                                         "summary": "3 строки"}


def test_forbidden_and_idempotent(store):
    tid = _task(store)
    with pytest.raises(tr.TransitionError):
        tr.move(store, tid, State.DONE)
    before = len(store.events())
    tr.move(store, tid, State.QUEUED)  # already there — no changes
    assert len(store.events()) == before
    with pytest.raises(tr.TransitionError):
        tr.move(store, 999, State.QUEUED)


def test_expect_from_guards_race(store):
    tid = _task(store)
    tr.move(store, tid, State.STOPPED)
    with pytest.raises(tr.ConflictError):
        tr.move(store, tid, State.REJECTED, expect_from={State.DONE})


def test_final_sets_finished(store):
    tid = _task(store)
    t = tr.move(store, tid, State.REJECTED, now=77)
    assert t.finished_at == 77
    with pytest.raises(tr.TransitionError):
        tr.move(store, tid, State.QUEUED)


def test_owner_protects_active_task(store):
    tid = _task(store)
    tr.move(store, tid, State.PREPARING)
    assert tr.acquire(store, tid, "A", pid=11, now=1000, lease_ms=500)
    assert not tr.acquire(store, tid, "B", pid=22, now=1100)  # a live lease
    with pytest.raises(tr.ConflictError, match="pid=11"):
        tr.move(store, tid, State.STOPPED, now=1100)  # a client without ownership
    with pytest.raises(tr.ConflictError):
        tr.move(store, tid, State.STOPPED, owner="B", now=1100)
    assert tr.renew(store, tid, "A", now=1400, lease_ms=500)  # until 1900
    assert not tr.acquire(store, tid, "B", pid=22, now=1800)
    # the lease expired — the service takes it, the old owner learns on the next renew
    assert tr.acquire(store, tid, "svc", pid=33, now=2000)
    assert not tr.renew(store, tid, "A", now=2001)
    t = tr.move(store, tid, State.QUEUED, owner="svc", reason="сирота", now=2002)
    assert t.owner == "" and t.state is State.QUEUED


def test_acquire_only_queued_or_active(store):
    tid = _task(store)
    tr.move(store, tid, State.REJECTED)
    assert not tr.acquire(store, tid, "A", pid=1)
    t2 = _task(store)
    tr.move(store, t2, State.STOPPED)
    assert not tr.acquire(store, t2, "A", pid=1)


def test_is_orphan(store):
    tid = _task(store)
    t = tr.move(store, tid, State.PREPARING)
    assert tr.is_orphan(t, now=0)
    tr.acquire(store, tid, "A", pid=1, now=100, lease_ms=50)
    assert not tr.is_orphan(store.get_task(tid), now=120)
    assert tr.is_orphan(store.get_task(tid), now=151)


def test_request_stop(store):
    q = _task(store)
    assert tr.request_stop(store, q) == "stopped"
    assert store.get_task(q).state is State.STOPPED
    assert tr.request_stop(store, q) == "stopped"  # a repeat

    a = _task(store)
    tr.move(store, a, State.PREPARING)
    tr.acquire(store, a, "own", pid=5, now=100)
    assert tr.request_stop(store, a, now=110) == "requested"
    t = store.get_task(a)
    assert t.request == "stop" and t.state is State.PREPARING
    t = tr.move(store, a, State.STOPPED, owner="own", now=120)
    assert t.request == ""

    d = _task(store)
    tr.move(store, d, State.REJECTED)
    with pytest.raises(tr.TransitionError):
        tr.request_stop(store, d)


def test_request_nudge_into_the_running_session(store):
    a = _task(store)
    tr.move(store, a, State.PREPARING)
    tr.acquire(store, a, "own", pid=5, now=100)
    store.add_session(task_id=a, provider="fake", role="executor", model="fake", external_id="ses_x", now=100)
    assert tr.request_nudge(store, a, text="хватит думать, почини\n", by="human", now=110) == "requested"
    t = store.get_task(a)
    assert t.request == "nudge" and t.request_text == "хватит думать, почини"  # the line is normalized
    assert t.state is State.PREPARING  # the owner decides, nobody else
    ev = store.events(task_id=a)[-1]
    assert ev.kind == "nudge" and ev.payload == {"text": "хватит думать, почини", "by": "human"}
    assert not ev.needs_reaction  # a journal entry, not a wake-up

    assert tr.clear_request(store, a, kind="nudge", text="другое") is False  # a newer message is not eaten
    assert tr.clear_request(store, a, kind="nudge", text="хватит думать, почини") is True
    assert store.get_task(a).request == "" and store.get_task(a).request_text == ""


def test_request_nudge_refused(store):
    q = _task(store)  # queued: no process, nothing to message
    with pytest.raises(tr.TransitionError, match="написать ему некого"):
        tr.request_nudge(store, q, text="почини")
    with pytest.raises(tr.TransitionError, match="сообщение пустое"):
        tr.request_nudge(store, q, text="  ")

    a = _task(store)
    tr.move(store, a, State.PREPARING)
    with pytest.raises(tr.TransitionError, match="нет живого процесса"):
        tr.request_nudge(store, a, text="почини", now=10)
    tr.acquire(store, a, "own", pid=5, now=100)
    with pytest.raises(tr.TransitionError, match="сессии ещё нет"):
        tr.request_nudge(store, a, text="почини", now=110)

    store.add_session(task_id=a, provider="fake", role="executor", model="fake", external_id="ses_x")
    assert tr.request_stop(store, a, now=110) == "requested"
    with pytest.raises(tr.TransitionError, match="остановка уже запрошена"):
        tr.request_nudge(store, a, text="почини", now=111)

    d = _task(store)
    tr.move(store, d, State.PREPARING)
    tr.move(store, d, State.WORKING)
    tr.move(store, d, State.DONE)  # finished: the worker is not there anymore
    with pytest.raises(tr.TransitionError, match="написать ему некого"):
        tr.request_nudge(store, d, text="почини")
    with pytest.raises(tr.TransitionError):
        tr.request_nudge(store, 999, text="почини")


def test_nudge_cleared_when_the_task_leaves_active(store):
    """The engine did not take the message — the task went on to "Needs decision"; the row is clean."""
    a = _task(store)
    tr.move(store, a, State.PREPARING)
    tr.acquire(store, a, "own", pid=5, now=100)
    store.add_session(task_id=a, provider="fake", role="executor", model="fake", external_id="ses_x")
    tr.request_nudge(store, a, text="почини", by="human", now=110)
    t = tr.move(store, a, State.NEEDS_DECISION, owner="own", reason="тишина", now=120)
    assert t.request == "" and t.request_text == ""


def test_cascade_on_reject(store):
    x = _task(store)
    y = _task(store, after=[x])
    z = _task(store, after=[x], state=State.DRAFT)
    tr.move(store, x, State.REJECTED, reason="не нужно")
    ty = store.get_task(y)
    assert ty.state is State.NEEDS_DECISION and f"T{x}" in ty.state_reason
    assert store.get_task(z).state is State.DRAFT
    assert Ev.NEEDS_DECISION in [e.kind for e in store.events(task_id=y, needs_reaction=True)]


def test_once_idempotent(store):
    calls = []

    def make(c):
        calls.append(1)
        return {"id": store.create_task(project="P", kind="scout", title="x", con=c)}

    r1 = tr.once(store, "cli:create:abc", make)
    r2 = tr.once(store, "cli:create:abc", make)
    assert r1 == r2 and len(calls) == 1
    assert len(store.list_tasks()) == 1


def test_once_rolls_back_with_action(store):
    def boom(c):
        store.create_task(project="P", kind="scout", title="x", con=c)
        raise RuntimeError("сбой")

    with pytest.raises(RuntimeError):
        tr.once(store, "k", boom)
    assert store.list_tasks() == []
    assert tr.once(store, "k", lambda c: 5) == 5  # the key is not busy after a failure


def _race(path, tid, token, q):
    s = Store(path)
    q.put(tr.acquire(s, tid, token, pid=0))


def test_acquire_race_one_winner(store):
    tid = _task(store)
    tr.move(store, tid, State.PREPARING)
    q = mp.get_context("fork").Queue()
    procs = [mp.get_context("fork").Process(target=_race, args=(store.path, tid, f"o{i}", q)) for i in range(6)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(20)
    wins = [q.get(timeout=5) for _ in procs]
    assert wins.count(True) == 1
