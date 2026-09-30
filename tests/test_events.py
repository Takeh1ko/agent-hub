from __future__ import annotations

import pytest

from ahub import events, transitions
from ahub.model import Ev, State
from ahub.store import Store


@pytest.fixture
def store() -> Store:
    return Store()


def done_task(store, title="найти утечку", now=1000, project="P", **payload):
    tid = store.create_task(project=project, kind="scout", title=title, now=now)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, tid, st, now=now)
    transitions.move(store, tid, State.DONE, reason="отчёт готов", now=now,
                     payload={"summary": "утечка в core/a.py", "report_bytes": 2150, "cost_go": 0.04, **payload})
    return tid


def test_grouping_window(store):
    tid = done_task(store, now=1000)
    assert events.ready_batch(store, now=1000 + 60_000) == []  # окно ещё не прошло
    batch = events.ready_batch(store, now=1000 + events.GROUP_WINDOW_MS)
    assert [e.kind for e in batch] == ["done"] and batch[0].task_id == tid


def test_immediate_pulls_whole_batch(store):
    done_task(store, now=1000)
    store.add_event(Ev.OWNER_MESSAGE, payload={"text": "как там оплата?"}, now=2000)
    batch = events.ready_batch(store, now=2001)
    assert [e.kind for e in batch] == ["done", "owner_message"]


def test_critical_alarm_immediate(store):
    store.add_event(Ev.ALARM, critical=True, payload={"text": "opencode недоступен 12 мин"}, now=5)
    assert events.lines(store, events.ready_batch(store, now=6)) == ["ТРЕВОГА! opencode недоступен 12 мин"]


def test_delivery_and_redelivery(store):
    done_task(store, now=1000)
    t = 1000 + events.GROUP_WINDOW_MS
    batch = events.ready_batch(store, now=t)
    events.mark_delivered(store, [e.id for e in batch], now=t)
    assert events.ready_batch(store, now=t + 1000) == []  # не будим тем же
    assert len(events.ready_batch(store, now=t + events.REDELIVER_MS + 1)) == 1  # не взяли — напомним
    assert events.ack(store) == 1
    assert events.ready_batch(store, now=t + 2 * events.REDELIVER_MS) == []


def test_ack_task_implicit(store):
    a = done_task(store)
    b = done_task(store, title="другая")
    assert events.ack_task(store, a) == 1
    assert [e.task_id for e in events.unacked(store)] == [b]


def test_project_filter(store):
    done_task(store, project="A")
    done_task(store, project="B")
    store.add_event(Ev.ALARM, critical=True, payload={"text": "общая"})  # без проекта — всем
    got = events.ready_batch(store, project="A", now=10**13)
    assert sorted(e.project for e in got) == ["", "A"]


def test_lines_format(store):
    tid = done_task(store)
    ev = store.events(task_id=tid, needs_reaction=True)[0]
    line = events.format_line(ev, store.get_task(tid))
    assert line.startswith(f"ГОТОВО T{tid} scout «найти утечку» — отчёт 2.1 КБ") and "$0.04" in line
    tid2 = store.create_task(project="P", kind="code", title="кнопка", now=1)
    transitions.move(store, tid2, State.REJECTED)
    store.add_event(Ev.ANSWER, payload={"question_id": 5, "question": "сливать T12?", "answer": "да"})
    assert events.lines(store, events.unacked(store))[-1] == "ОТВЕТ #5 «сливать T12?» → да"
    long = store.add_event(Ev.OWNER_MESSAGE, payload={"text": "а" * 500})
    assert len(events.format_line(store.events(after_id=long - 1)[0], None)) <= events.LINE_LIMIT


def test_presence(store):
    assert not events.present(store)
    events.touch(store, project="P", via="wait", now=1000)
    assert events.present(store, now=1000 + 60_000)
    assert not events.present(store, now=1000 + events.PRESENT_MS + 1)
    events.touch(store, via="watch", now=2000)  # проект не затирается пустым
    assert events.presence(store)["project"] == "P"


def test_wait_returns_batch_and_marks(store):
    clock = {"t": 0.0}
    done_task(store, now=0)  # старое — окно давно прошло
    got = events.wait(store, timeout_s=5, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                      clock=lambda: clock["t"])
    assert len(got) == 1 and got[0].startswith("ГОТОВО")
    assert events.present(store)
    assert events.wait(store, timeout_s=3, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                       clock=lambda: clock["t"]) == []  # уже доставлено — ждём до таймаута


def test_redelivery_capped(store):
    done_task(store, now=0)
    t = events.GROUP_WINDOW_MS
    for i in range(events.MAX_DELIVERIES):
        batch = events.ready_batch(store, now=t)
        assert len(batch) == 1, i
        events.mark_delivered(store, [batch[0].id], now=t)
        t += events.REDELIVER_MS + 1
    assert events.ready_batch(store, now=t) == []  # дальше — только «непрочитано» в status
    assert len(events.unacked(store)) == 1
