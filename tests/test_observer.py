from __future__ import annotations

import json
import os
import sys

import pytest

from ahub import comms, events, log, observer, registry, transitions
from ahub.model import Role, State
from ahub.store import Store
from ahub.time import now_ms
from tests.enginekit import install_fake


def _write_log(ts: int, msg: str, *, lvl: str = "ERROR", comp: str = "tg", pid: int = 1650) -> None:
    """A log record with the given ts/pid (to test the window and dead pids)."""
    p = log.log_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts, "lvl": lvl, "comp": comp, "msg": msg, "pid": pid}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


@pytest.fixture
def store() -> Store:
    s = Store()
    s.meta_set("service_heartbeat", str(now_ms()))
    return s


@pytest.fixture(autouse=True)
def _no_real_providers(monkeypatch):
    """Tests never touch real providers: quick_check calls health() on the machine, and a real
    WARNING (agy models exiting 1) then lands in the faked log and shows up in the next cycle."""
    from ahub import providers
    from ahub.providers.base import Health

    class _Healthy:
        name = "opencode"

        def health(self) -> Health:
            return Health(True, ())

    monkeypatch.setattr(providers, "names", lambda: ["opencode"])
    monkeypatch.setitem(providers._cache, "opencode", _Healthy())


def qc(store, **kw):
    """live={} — hermetic: the pulse of a test task is ⚫ whatever else runs on this machine."""
    return observer.quick_check(store, projects=[], health=False, live={}, **kw)


def test_clean(store):
    assert qc(store) == []


def test_dead_task_and_logs(store):
    tid = store.create_task(project="P", kind="scout", title="x")
    transitions.move(store, tid, State.PREPARING)
    with store.tx() as c:  # long without a process — the orphan grace is over
        c.execute("UPDATE task SET updated_at=0 WHERE id=?", (tid,))
    log.setup()
    log.get("engine").error("сбой шага T%d: 500", tid)
    sus = qc(store, since=0)
    texts = " | ".join(s.text for s in sus)
    assert f"T{tid} ⚫" in texts and "ERROR engine: сбой шага" in texts


def test_heartbeat_stale_is_critical(store):
    store.meta_set("service_heartbeat", str(now_ms() - 10 * 60_000))
    sus = qc(store)
    assert sus and sus[0].critical and "сервис не тикает" in sus[0].text


def test_broken_provider_is_critical(store, monkeypatch):
    """quick_check calls health() of every provider — here a stub, never the machine."""
    from ahub import providers
    from ahub.providers.base import Health

    class _Broken:
        name = "opencode"

        def health(self) -> Health:
            return Health(False, ("no login",))

    monkeypatch.setitem(providers._cache, "opencode", _Broken())
    sus = observer.quick_check(store, projects=[], health=True, live={})
    assert [s.sig for s in sus] == ["health:opencode"] and sus[0].critical
    assert sus[0].text.endswith("no login")
    assert qc(store) == []  # the same check without the health part — clean


def test_queue_stuck_and_unacked(store):
    tid = store.create_task(project="P", kind="scout", title="x", now=now_ms() - 30 * 60_000)
    store.add_event("done", task_id=tid, now=now_ms() - 30 * 60_000)
    texts = " | ".join(s.text for s in qc(store))
    assert f"T{tid} в очереди" in texts and "ждут оркестратора" in texts
    events.touch(store)  # Claude is listening — unread events are not a suspicion
    assert "ждут оркестратора" not in " | ".join(s.text for s in qc(store))


def test_cycle_without_model_raises_once(store):
    store.meta_set("service_heartbeat", str(now_ms() - 10 * 60_000))
    store.meta_set(observer.LAST_DEEP, str(now_ms()))
    assert observer.cycle(store, use_model=False, projects=[]) == "critical"
    al = comms.alarms(store)
    assert len(al) == 1 and al[0].critical
    assert observer.cycle(store, use_model=False, projects=[]) == "ok"  # the same problem — pause
    assert len(comms.alarms(store)) == 1
    assert observer.reports(store, 1)[0]["kind"] == "quick"


def _stale_heartbeat(store) -> None:
    """A code suspicion of its own: the hub looks alive to nobody (the service tick is old)."""
    store.meta_set("service_heartbeat", str(now_ms() - 10 * 60_000))


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
    _stale_heartbeat(store)  # a suspicion of its own — otherwise the cycle never reaches the model
    assert observer.cycle(store, projects=[]) == "false_alarm"
    assert comms.alarms(store) == []
    assert "Code suspicions" in fake.calls[0]["prompt"] and f"T{tid}" in fake.calls[0]["prompt"]
    assert observer.reports(store, 1)[0]["cost_go"] == pytest.approx(0.002)


def test_triage_alarm(store):
    _observer_fake(store, '```json\n{"verdict": "alarm", "summary": "T1 висит", "action": "перезапустить"}\n```')
    tid = store.create_task(project="P", kind="scout", title="x")
    transitions.move(store, tid, State.PREPARING)
    store.meta_set(observer.LAST_DEEP, str(now_ms()))
    _stale_heartbeat(store)
    assert observer.cycle(store, projects=[]) == "alarm"
    assert events.lines(store, comms.alarms(store)) == ["ALARM T1 висит → перезапустить"]


def test_deep_runs_even_when_clean(store):
    fake = _observer_fake(store, '{"verdict": "ok", "summary": "всё штатно"}')
    assert observer.cycle(store, projects=[]) == "ok"  # no LAST_DEEP — the scheduled run
    assert "checklist" in fake.calls[0]["prompt"]
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
    assert not observer.watchdog(store)  # does not repeat
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


def test_pause_by_signature_ignores_counters(store):
    s1 = observer.Suspicion("pulse:1:silent", "T1 молчит 21 мин")
    s2 = observer.Suspicion("pulse:1:silent", "T1 молчит 26 мин")
    t0 = now_ms()
    observer._mark_seen(store, [s1], t0)
    assert observer._fresh(store, [s2], t0 + 5 * 60_000) == []
    assert observer._fresh(store, [s2], t0 + observer.REPEAT_MS + 1) == [s2]


def test_dead_within_orphan_grace_not_suspicious(store):
    tid = store.create_task(project="P", kind="scout", title="x")
    transitions.move(store, tid, State.PREPARING)  # just now — the service will still pick it up
    assert not any(f"T{tid}" in s.text for s in qc(store))


def test_proxy_problem():
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert observer.proxy_problem({"HTTPS_PROXY": f"http://127.0.0.1:{port}"}) == ""
    finally:
        srv.close()
    assert "не отвечает" in observer.proxy_problem({"HTTPS_PROXY": f"http://127.0.0.1:{port}"})
    assert observer.proxy_problem({}) == ""


def test_unit_carries_proxy(monkeypatch, capsys):
    from ahub import cli
    monkeypatch.setattr(sys, "platform", "linux")  # check the proxy in a systemd unit
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    assert cli.main(["service", "install", "--print"]) == 0
    assert 'Environment="HTTPS_PROXY=http://127.0.0.1:8080"' in capsys.readouterr().out


def test_deep_prompt_has_window_and_ignores_old(store):
    fake = _observer_fake(store, '{"verdict": "ok", "summary": "чисто"}')
    now = now_ms()
    win = now - 10 * 60_000
    store.meta_set(observer.LAST_DEEP, str(win))
    _write_log(win - 60_000, "polling упал СТАРАЯ-УНИКАЛЬНАЯ-12345", pid=1650)
    _write_log(win + 60_000, "polling упал НОВАЯ-УНИКАЛЬНАЯ-67890", pid=os.getpid())
    assert observer.triage(store, [], deep=True, now=now)["verdict"] == "ok"
    prompt = fake.calls[0]["prompt"]
    assert str(win) in prompt and "look only at log records" in prompt
    assert "НОВАЯ-УНИКАЛЬНАЯ-67890" in prompt
    assert "СТАРАЯ-УНИКАЛЬНАЯ-12345" not in prompt


def test_triage_suspicion_window_like_quick(store):
    fake = _observer_fake(store, '{"verdict": "ok", "summary": "чисто"}')
    now = now_ms()
    win = now - 4 * 60_000
    store.meta_set(observer.LAST_DEEP, str(now))  # scheduled run not due — triage suspicions
    store.meta_set(observer.LAST_QUICK, str(win))
    _write_log(win - 60_000, "сбой СТАРАЯ-ПОДОЗРЕНИЕ-111", pid=1650)
    _write_log(win + 60_000, "сбой НОВАЯ-ПОДОЗРЕНИЕ-222", pid=os.getpid())
    sus = [observer.Suspicion("test:win", "тестовое подозрение")]
    observer.triage(store, sus, deep=False, now=now, since=win)
    prompt = fake.calls[0]["prompt"]
    assert str(win) in prompt
    assert "НОВАЯ-ПОДОЗРЕНИЕ-222" in prompt
    assert "СТАРАЯ-ПОДОЗРЕНИЕ-111" not in prompt


def test_log_digest_size_limit(store):
    now = now_ms()
    since = now - 30 * 60_000
    for i in range(50):
        _write_log(now - 1000 + i, "длинная ошибка " + "Ы" * 200, comp=f"cmp{i}", pid=1_000_000 + i)
    d = observer.log_digest(since, now=now)
    assert len(d.encode("utf-8")) <= observer.LOG_DIGEST_BYTES


def test_dead_pid_marked_and_snapshot_live(store):
    now = now_ms()
    since = now - 5 * 60_000
    dead_pid = 1_000_000_007  # no such process
    _write_log(now - 1000, "polling упал МЁРТВЫЙ-ТЕСТ-ПИД", pid=dead_pid)
    _write_log(now - 500, "polling упал ЖИВОЙ-ТЕСТ-ПИД", pid=os.getpid())
    d = observer.log_digest(since, now=now)
    assert "МЁРТВЫЙ-ТЕСТ-ПИД" in d and "мёртвый pid" in d
    for line in d.splitlines():
        if "ЖИВОЙ-ТЕСТ-ПИД" in line:
            assert "мёртвый" not in line
    snap = observer.snapshot(store, now=now)
    assert f"сервис {os.getpid()}" in snap
