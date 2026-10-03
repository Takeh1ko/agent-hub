from __future__ import annotations

import pytest

from ahub import events, reasons, transitions
from ahub.i18n import _reset
from ahub.model import Ev, State
from ahub.scope import OWNER, Scope
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
    assert events.ready_batch(store, now=1000 + 60_000) == []  # the window has not passed yet
    batch = events.ready_batch(store, now=1000 + events.GROUP_WINDOW_MS)
    assert [e.kind for e in batch] == ["done"] and batch[0].task_id == tid


def test_immediate_pulls_whole_batch(store):
    done_task(store, now=1000)
    store.add_event(Ev.OWNER_MESSAGE, payload={"text": "как там оплата?"}, now=2000)
    batch = events.ready_batch(store, now=2001)
    assert [e.kind for e in batch] == ["done", "owner_message"]


def test_critical_alarm_immediate(store):
    store.add_event(Ev.ALARM, critical=True, payload={"text": "opencode недоступен 12 мин"}, now=5)
    assert events.lines(store, events.ready_batch(store, now=6)) == ["ALARM! opencode недоступен 12 мин"]


def test_delivery_and_redelivery(store):
    done_task(store, now=1000)
    t = 1000 + events.GROUP_WINDOW_MS
    batch = events.ready_batch(store, now=t)
    events.mark_delivered(store, [e.id for e in batch], now=t)
    assert events.ready_batch(store, now=t + 1000) == []  # do not wake it again
    assert len(events.ready_batch(store, now=t + events.REDELIVER_MS + 1)) == 1  # not taken — remind
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
    store.add_event(Ev.ALARM, critical=True, payload={"text": "общая"})  # no project — for everyone
    got = events.ready_batch(store, scope=Scope(("A",)), now=10**13)
    assert sorted(e.project for e in got) == ["", "A"]
    assert len(events.ready_batch(store, scope=OWNER, now=10**13)) == 3  # the owner sees every project


def test_lines_format(store):
    tid = done_task(store)
    ev = store.events(task_id=tid, needs_reaction=True)[0]
    line = events.format_line(ev, store.get_task(tid))
    assert line.startswith(f"DONE T{tid} scout «найти утечку» — отчёт 2.1 КБ") and "$0.04" in line
    tid2 = store.create_task(project="P", kind="code", title="кнопка", now=1)
    transitions.move(store, tid2, State.REJECTED)
    store.add_event(Ev.ANSWER, payload={"question_id": 5, "question": "сливать T12?", "answer": "да"})
    assert events.lines(store, events.unacked(store))[-1] == "ANSWER #5 «сливать T12?» → да"
    long = store.add_event(Ev.OWNER_MESSAGE, payload={"text": "а" * 500})
    assert len(events.format_line(store.events(after_id=long - 1)[0], None)) <= events.LINE_LIMIT


def test_presence_is_per_project(store):
    assert not events.present(store)
    events.touch(store, project="P", via="wait", now=1000)
    assert events.present(store, project="P", now=1000 + 60_000)  # a live session in P
    assert not events.present(store, project="B", now=1000 + 60_000)  # … says nothing about B
    assert not events.present(store, project="P", now=1000 + events.PRESENT_MS + 1)
    events.touch(store, project="B", via="watch", now=1000 + 1000)  # two sessions of one `who`
    assert events.present(store, project="P", now=1000 + 2000)
    assert events.present(store, project="B", now=1000 + 2000)
    assert events.present(store, now=1000 + 2000)  # no project — any of them
    assert events.presence(store, project="P")["via"] == "wait" and events.presence(store)["project"] == "B"


def test_touch_scope_of_the_owner_stamps_every_project(store, tmp_path, monkeypatch):
    from ahub import paths
    from ahub.scope import Scope
    from tests.conftest import write

    roots = {}
    for name in ("A", "B"):
        roots[name] = write(tmp_path / name.lower() / ".hub.toml",
                            f'schema_version = 2\nname = "{name}"\n').parent
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text(f'projects = ["{roots["A"]}", "{roots["B"]}"]\n', encoding="utf-8")

    events.touch_scope(store, Scope(("A",)), via="watch", now=1000)
    assert events.present(store, project="A", now=1000) and not events.present(store, project="B", now=1000)
    events.touch_scope(store, Scope(), via="watch", now=2000)  # the owner — every project
    assert events.present(store, project="A", now=2000) and events.present(store, project="B", now=2000)


def test_wait_returns_batch_and_marks(store):
    clock = {"t": 0.0}
    done_task(store, now=0)  # old — the window passed long ago
    got = events.wait(store, timeout_s=5, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                      clock=lambda: clock["t"])
    assert len(got) == 1 and got[0].startswith("DONE")
    assert events.present(store)
    assert events.wait(store, timeout_s=3, sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
                       clock=lambda: clock["t"]) == []  # already delivered — wait until the timeout


def test_redelivery_capped(store):
    done_task(store, now=0)
    t = events.GROUP_WINDOW_MS
    for i in range(events.MAX_DELIVERIES):
        batch = events.ready_batch(store, now=t)
        assert len(batch) == 1, i
        events.mark_delivered(store, [batch[0].id], now=t)
        t += events.REDELIVER_MS + 1
    assert events.ready_batch(store, now=t) == []  # from now on only "unread" in status
    assert len(events.unacked(store)) == 1


def test_watch_summary_once(store):
    done_task(store, now=0)
    batch = events.ready_batch(store, now=events.GROUP_WINDOW_MS)
    events.mark_delivered(store, [e.id for e in batch], now=events.GROUP_WINDOW_MS)
    first = events.watch_start_summary(store)
    assert len(first) == 1
    assert len(events.unacked(store)) == 1  # old stays unread
    assert events.watch_start_summary(store) == []  # restart stays silent
    assert events.watch_start_summary(store) == []  # still silent


def test_watch_summary_new_event_again(store):
    done_task(store, title="первая", now=0)
    batch = events.ready_batch(store, now=events.GROUP_WINDOW_MS)
    events.mark_delivered(store, [e.id for e in batch], now=events.GROUP_WINDOW_MS)
    assert len(events.watch_start_summary(store)) == 1
    assert events.watch_start_summary(store) == []
    done_task(store, title="вторая", now=1000)
    batch = events.ready_batch(store, now=1000 + events.GROUP_WINDOW_MS)
    assert len(batch) == 1 and batch[0].task_id == 2
    events.mark_delivered(store, [e.id for e in batch], now=1000 + events.GROUP_WINDOW_MS)
    again = events.watch_start_summary(store)
    assert len(again) == 1 and again[0].task_id == 2  # only the new one, no repeat of the old
    assert events.watch_start_summary(store) == []
    assert len(events.unacked(store)) == 2  # both stay unread


def test_watch_summary_ack(store):
    done_task(store, now=0)
    batch = events.ready_batch(store, now=events.GROUP_WINDOW_MS)
    events.mark_delivered(store, [e.id for e in batch], now=events.GROUP_WINDOW_MS)
    assert len(events.watch_start_summary(store)) == 1
    assert events.ack(store) == 1
    assert events.unacked(store) == []
    assert events.watch_start_summary(store) == []  # acked — nothing to announce


def test_watch_summary_per_who(store):
    done_task(store, now=0)
    batch = events.ready_batch(store, now=events.GROUP_WINDOW_MS)
    events.mark_delivered(store, [e.id for e in batch], now=events.GROUP_WINDOW_MS)
    assert len(events.watch_start_summary(store, who="claude")) == 1
    assert events.watch_start_summary(store, who="claude") == []
    assert len(events.watch_start_summary(store, who="other")) == 1  # another consumer hears it once
    assert events.watch_start_summary(store, who="other") == []


def test_a_stored_reason_blob_is_rendered_in_the_line(store, monkeypatch):
    """A NEEDS_DECISION/ERROR line must never show the stored JSON — the reader gets a sentence."""
    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    decided = store.create_task(project="P", kind="code", title="pay button", now=0)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, decided, st, now=0)
    transitions.move(store, decided, State.NEEDS_DECISION,
                     reason=reasons.dump("review_exhausted", n=2, highs=1), now=0)
    failed = store.create_task(project="P", kind="code", title="pay button", now=0)
    transitions.move(store, failed, State.PREPARING, now=0)
    transitions.move(store, failed, State.ERROR, reason=reasons.dump("quota", err="window is over"), now=0)
    lines = events.lines(store, [e for e in store.events(needs_reaction=True)
                                 if e.kind in ("needs_decision", "error")])
    assert lines == ["DECISION T1 code «pay button» — review rounds exhausted (2 findings, high: 1)",
                     "ERROR T2 code «pay button» — provider quota: window is over"]
    assert not any('{"code"' in ln for ln in lines)


def test_a_long_reason_is_clipped_at_a_word(store, monkeypatch):
    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    tid = store.create_task(project="P", kind="scout", title="x", now=0)
    store.add_event(Ev.NEEDS_DECISION, task_id=tid, project="P", now=1,
                    payload={"reason": reasons.dump("blocked", summary="the worker needs a database login "
                                                                     "that nobody has " * 3)})
    line = events.lines(store, store.events(task_id=tid, needs_reaction=True))[0]
    assert "…" in line and "nobodyha" not in line and len(line) <= events.LINE_LIMIT
