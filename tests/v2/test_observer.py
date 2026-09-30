from __future__ import annotations

import json

import pytest

from ahub import comms, events, log, observer, registry, transitions
from ahub.model import Role, State
from ahub.store import Store
from ahub.time import now_ms
from tests.v2.enginekit import install_fake


@pytest.fixture
def store() -> Store:
    s = Store()
    s.meta_set("service_heartbeat", str(now_ms()))
    return s


def qc(store, **kw):
    return observer.quick_check(store, projects=[], health=False, **kw)


def test_clean(store):
    assert qc(store) == []


def test_dead_task_and_logs(store):
    tid = store.create_task(project="P", kind="scout", title="x")
    transitions.move(store, tid, State.PREPARING)
    log.setup()
    log.get("engine").error("сбой шага T%d: 500", tid)
    sus = qc(store, since=0)
    texts = " | ".join(s.text for s in sus)
    assert f"T{tid} ⚫" in texts and "ERROR engine: сбой шага" in texts


def test_heartbeat_stale_is_critical(store):
    store.meta_set("service_heartbeat", str(now_ms() - 10 * 60_000))
    sus = qc(store)
    assert sus and sus[0].critical and "сервис не тикает" in sus[0].text


def test_queue_stuck_and_unacked(store):
    tid = store.create_task(project="P", kind="scout", title="x", now=now_ms() - 30 * 60_000)
    store.add_event("done", task_id=tid, now=now_ms() - 30 * 60_000)
    texts = " | ".join(s.text for s in qc(store))
    assert f"T{tid} в очереди" in texts and "ждут оркестратора" in texts
    events.touch(store)  # Claude слушает — неразобранное не тревога
    assert "ждут оркестратора" not in " | ".join(s.text for s in qc(store))


def test_cycle_without_model_raises_once(store):
    store.meta_set("service_heartbeat", str(now_ms() - 10 * 60_000))
    store.meta_set(observer.LAST_DEEP, str(now_ms()))
    assert observer.cycle(store, use_model=False, projects=[]) == "critical"
    al = comms.alarms(store)
    assert len(al) == 1 and al[0].critical
    assert observer.cycle(store, use_model=False, projects=[]) == "ok"  # та же проблема — пауза
    assert len(comms.alarms(store)) == 1
    assert observer.reports(store, 1)[0]["kind"] == "quick"


def _observer_fake(store, answer: str):
    fake = install_fake(store, [{"session": "ses_obs", "steps": [
        {"event": {"type": "usage", "in": 50, "out": 20, "go": 0.002}},
        {"event": {"type": "text", "text": answer}}]}])
    registry.add_to_role(store, Role.OBSERVER, "fake", default=True)
    return fake


def test_triage_false_alarm_no_alarm(store):
    fake = _observer_fake(store, json.dumps({"verdict": "false_alarm", "summary": "идут долгие тесты"}))
    tid = store.create_task(project="P", kind="scout", title="x")
    transitions.move(store, tid, State.PREPARING)
    store.meta_set(observer.LAST_DEEP, str(now_ms()))
    assert observer.cycle(store, projects=[]) == "false_alarm"
    assert comms.alarms(store) == []
    assert "Подозрения кода" in fake.calls[0]["prompt"] and f"T{tid}" in fake.calls[0]["prompt"]
    assert observer.reports(store, 1)[0]["cost_go"] == pytest.approx(0.002)


def test_triage_alarm(store):
    _observer_fake(store, '```json\n{"verdict": "alarm", "summary": "T1 висит", "action": "перезапустить"}\n```')
    tid = store.create_task(project="P", kind="scout", title="x")
    transitions.move(store, tid, State.PREPARING)
    store.meta_set(observer.LAST_DEEP, str(now_ms()))
    assert observer.cycle(store, projects=[]) == "alarm"
    assert events.lines(store, comms.alarms(store)) == ["ТРЕВОГА T1 висит → перезапустить"]


def test_deep_runs_even_when_clean(store):
    fake = _observer_fake(store, '{"verdict": "ok", "summary": "всё штатно"}')
    assert observer.cycle(store, projects=[]) == "ok"  # LAST_DEEP нет → плановая
    assert "чек-лист" in fake.calls[0]["prompt"]
    assert observer.reports(store, 1)[0]["kind"] == "deep"


def test_model_silent_but_code_critical(store):
    _observer_fake(store, "не json")
    store.meta_set("service_heartbeat", str(now_ms() - 10 * 60_000))
    assert observer.cycle(store, projects=[]) == "critical"
    assert "модель наблюдателя не ответила" in comms.alarms(store)[0].payload["text"]


def test_watchdog(store):
    assert not observer.watchdog(store)
    store.meta_set(observer.LAST_QUICK, str(now_ms() - 20 * 60_000))
    assert observer.watchdog(store)
    assert not observer.watchdog(store)  # не повторяет
    assert "наблюдатель не проверял" in comms.alarms(store)[0].payload["text"]


def test_alarms_for_tg(store):
    t0 = now_ms()
    crit = comms.raise_alarm(store, "лёг", critical=True, now=t0)
    norm = comms.raise_alarm(store, "висит", now=t0)
    acked = comms.raise_alarm(store, "разобрано", now=t0)
    events.ack(store, [acked])
    assert [e.id for e in comms.alarms_for_tg(store, now=t0 + 1000)] == [crit]
    assert [e.id for e in comms.alarms_for_tg(store, now=t0 + comms.ESCALATE_MS)] == [crit, norm]
    comms.mark_tg_sent(store, [crit, norm])
    assert comms.alarms_for_tg(store, now=t0 + 10 ** 8) == []
