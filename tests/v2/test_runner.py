"""Общий раннер поставщика на фейковом поставщике: поток, id, тишина, дети, таймаут, остановка, сбои."""

from __future__ import annotations

import json
import time

import pytest

from ahub import providers
from ahub.providers.base import Act, Outcome, RunSpec
from ahub.providers.fake import FakeProvider
from ahub.providers.runner import run
from tests.v2 import provider_contract as contract


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider()


def spec(tmp_path, scenario: dict, **kw) -> RunSpec:
    return RunSpec(prompt=json.dumps(scenario), cwd=str(tmp_path), model_id="fake/model",
                   log_path=str(tmp_path / "run.log"), **kw)


def _make_spec(tmp_path):
    def make(kind: str, cwd: str) -> RunSpec:
        text = "PONG" if kind == "hello" else "PONG2"
        return spec(tmp_path, {"session": "ses_c", "steps": [
            {"event": {"type": "step"}},
            {"event": {"type": "usage", "in": 10, "out": 3, "go": 0.001}},
            {"event": {"type": "text", "text": text}}]})
    return make


def test_contract_fake(fake, tmp_path):
    contract.check_catalog_and_health(fake)
    contract.check_catalog_and_health(FakeProvider(healthy=False))
    contract.check_session_cycle(fake, _make_spec(tmp_path), str(tmp_path))


def test_ok_stream_session_usage_log(fake, tmp_path):
    acts, sids, pids = [], [], []
    r = run(fake, spec(tmp_path, {"session": "ses_1", "steps": [
        {"event": {"type": "tool_start", "tool": "bash"}},
        {"event": {"type": "tool_end", "tool": "bash"}},
        {"event": {"type": "usage", "in": 100, "out": 20, "go": 0.01}},
        {"event": {"type": "usage", "in": 50, "out": 5, "go": 0.005}},
        {"event": {"type": "text", "text": "черновик"}},
        {"event": {"type": "text", "text": "итог"}}]}),
        on_activity=acts.append, on_session=sids.append, on_start=pids.append)
    assert r.ok and r.session_id == "ses_1" and sids == ["ses_1"]
    assert pids and r.exit_code == 0
    assert r.final_text == "итог"
    assert r.usage.tokens_in == 150 and round(r.usage.cost_go, 4) == 0.015
    assert [a.kind for a in acts if a.kind is not Act.SESSION][:2] == [Act.TOOL_START, Act.TOOL_END]
    log = (tmp_path / "run.log").read_text()
    assert '"sessionID": "ses_1"' in log and "итог" in log
    assert r.activities == len(acts)


def test_structured(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "text", "text": '```json\n{"verdict": "approve"}\n```'}}]}, schema={"type": "object"}))
    assert r.structured == {"verdict": "approve"}


def test_resume_keeps_session(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "new", "steps": [{"event": {"type": "text", "text": "x"}}]},
                       session_id="ses_old"))
    assert r.session_id == "ses_old"


def test_silence_without_children(fake, tmp_path):
    t0 = time.monotonic()
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [{"sleep": 10}]}, idle_s=1))
    assert r.outcome is Outcome.SILENCE and r.silence_s >= 1
    assert time.monotonic() - t0 < 8
    assert r.session_id == "s"  # id, пойманный до тишины, — для продолжения


def test_silence_explained_by_child(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"child": 2.5}, {"event": {"type": "text", "text": "тесты прошли"}}]}, idle_s=1))
    assert r.ok and r.final_text == "тесты прошли"


def test_timeout(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [{"child": 20}]}, timeout_s=1, idle_s=0))
    assert r.outcome is Outcome.TIMEOUT


def test_should_stop_kills_group(fake, tmp_path):
    pids = []
    flag = {"stop": False}
    r_holder = {}

    def stopper():
        return flag["stop"]

    import threading
    th = threading.Thread(target=lambda: r_holder.setdefault("r", run(
        fake, spec(tmp_path, {"session": "s", "steps": [{"child": 30}]}, idle_s=0),
        on_start=pids.append, should_stop=stopper)))
    th.start()
    time.sleep(1.0)
    from ahub import procs
    kids = procs.children(pids[0])
    assert kids, "ребёнок не появился"
    flag["stop"] = True
    th.join(20)
    assert r_holder["r"].outcome is Outcome.KILLED
    time.sleep(0.3)
    assert not any(procs.alive(k) for k in kids), "дети пережили остановку"


def test_transient_error(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "error", "message": "Unexpected server error"}}], "exit": 1}))
    assert r.outcome is Outcome.TRANSIENT and "server" in r.error


def test_transient_but_rc0_with_session_is_ok(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "error", "message": "status 502"}},
        {"event": {"type": "text", "text": "всё же сделал"}}], "exit": 0}))
    assert r.ok


def test_transient_then_silence_is_transient(fake, tmp_path):
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "error", "message": "ECONNREFUSED"}}, {"sleep": 10}]}, idle_s=1))
    assert r.outcome is Outcome.TRANSIENT


def test_quota_and_no_access(fake, tmp_path):
    q = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "error", "message": "Rate limit: quota exceeded"}}], "exit": 1}))
    assert q.outcome is Outcome.QUOTA
    a = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "error", "message": "401 Unauthorized"}}], "exit": 1}))
    assert a.outcome is Outcome.NO_ACCESS


def test_model_error_and_crash(fake, tmp_path):
    e = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"event": {"type": "error", "message": "invalid request: bad tool"}}], "exit": 1}))
    assert e.outcome is Outcome.MODEL_ERROR
    c = run(fake, spec(tmp_path, {"session": "s", "steps": [{"stderr": "segfault"}, {"crash": True}]}))
    assert c.outcome is Outcome.CRASH and "segfault" in c.error
    n = run(fake, spec(tmp_path, {"hide_session": True, "steps": [{"event": {"type": "text", "text": "x"}}]}))
    assert n.outcome is Outcome.CRASH and "нет id" in n.error  # id не пойман и не найден — честно


def test_not_started(tmp_path):
    class Broken(FakeProvider):
        def build_command(self, spec):
            return ["/nonexistent/binary-xyz"]

    r = run(Broken(), spec(tmp_path, {}))
    assert r.outcome is Outcome.NOT_STARTED


def test_callback_errors_do_not_break(fake, tmp_path):
    def bad(_):
        raise RuntimeError("колбэк упал")

    r = run(fake, spec(tmp_path, {"session": "s", "steps": [{"event": {"type": "text", "text": "ok"}}]}),
            on_activity=bad, on_session=bad, on_start=bad)
    assert r.ok


def test_registry():
    assert "opencode" in providers.names() and "fake" not in providers.names()
    providers.register("fake", FakeProvider())
    assert providers.get("fake").name == "fake"
    with pytest.raises(KeyError):
        providers.get("nope")
