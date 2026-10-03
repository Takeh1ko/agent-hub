"""The shared provider runner against the fake provider: stream, id, silence, children, timeout, stop, failures."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from ahub import paths, providers
from ahub.providers.base import Act, Outcome, RunSpec
from ahub.providers.fake import FakeProvider
from ahub.providers.runner import run
from tests import provider_contract as contract
from tests.conftest import write


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
    # the id is not visible in the stream (hide_session) — fallback path: it kept ses_old, so that is it
    r = run(fake, spec(tmp_path, {"hide_session": True, "steps": [{"event": {"type": "text", "text": "x"}}]},
                       session_id="ses_old"))
    assert r.ok and r.session_id == "ses_old"


def test_merge_usage_takes_larger():
    from ahub.providers.base import Usage
    from ahub.providers.runner import merge_usage
    db = Usage(tokens_in=100, cost_go=0.01)
    stream = Usage(tokens_in=150, cost_go=0.005, tokens_out=7)
    m = merge_usage(db, stream)
    assert m.tokens_in == 150 and m.cost_go == 0.01 and m.tokens_out == 7
    assert merge_usage(None, stream) is stream and merge_usage(db, None) is db


def test_garbage_output_does_not_reset_silence(fake, tmp_path):
    steps = [{"sleep": 0.3}, {"event": {"type": "noise"}}] * 10
    r = run(fake, spec(tmp_path, {"hide_session": True, "steps": steps}, idle_s=1))
    assert r.outcome is Outcome.SILENCE  # lines arrive every 0.3 s, but unrecognized ones are not life


def test_silence_without_children(fake, tmp_path):
    t0 = time.monotonic()
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [{"sleep": 10}]}, idle_s=1))
    assert r.outcome is Outcome.SILENCE and r.silence_s >= 1
    assert time.monotonic() - t0 < 8
    assert r.session_id == "s"  # the id caught before the silence — for resuming


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
    assert n.outcome is Outcome.CRASH and "нет id" in n.error  # the id was neither caught nor found — honest


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


_DUMP_PROXY_ENV = ("import json, os;"
                   "print(json.dumps({'sessionID': 'ses_env'}));"
                   "print(json.dumps({'type': 'text', 'text': json.dumps("
                   "{k: v for k, v in os.environ.items() if 'PROXY' in k.upper()})}))")


class EnvProvider(FakeProvider):
    """A provider whose process prints the proxy variables it actually got."""

    name = "opencode"

    def build_command(self, spec):
        return [sys.executable, "-c", _DUMP_PROXY_ENV]


def _proxy_env_of_the_process(tmp_path, monkeypatch, section: str) -> dict[str, str]:
    write(paths.global_config_path(), section)
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        for var in (name, name.lower()):
            monkeypatch.setenv(var, f"http://inherited-{name.lower()}:9")
    r = run(EnvProvider(), spec(tmp_path, {}))
    assert r.ok and r.session_id == "ses_env"
    return json.loads(r.final_text)


def test_provider_process_proxy_from_config(tmp_path, monkeypatch):
    """[providers.<name>] decides the proxy variables of that provider's process (the hub's own — no)."""
    env = _proxy_env_of_the_process(tmp_path, monkeypatch, """
[providers.opencode]
proxy = "http://127.0.0.1:8080"
no_proxy = "localhost"
""")
    assert env == {"HTTPS_PROXY": "http://127.0.0.1:8080", "https_proxy": "http://127.0.0.1:8080",
                   "HTTP_PROXY": "http://127.0.0.1:8080", "http_proxy": "http://127.0.0.1:8080",
                   "ALL_PROXY": "http://127.0.0.1:8080", "all_proxy": "http://127.0.0.1:8080",
                   "NO_PROXY": "localhost", "no_proxy": "localhost"}
    assert os.environ["HTTPS_PROXY"] == "http://inherited-https_proxy:9"  # the hub's own processes are untouched


def test_provider_process_proxy_empty_means_none(tmp_path, monkeypatch):
    """proxy = "" drops only the proxy URL variables; no_proxy is absent — it stays inherited."""
    env = _proxy_env_of_the_process(tmp_path, monkeypatch, '[providers.opencode]\nproxy = ""\n')
    assert env == {"NO_PROXY": "http://inherited-no_proxy:9", "no_proxy": "http://inherited-no_proxy:9"}


def test_provider_process_without_section_inherits(tmp_path, monkeypatch):
    env = _proxy_env_of_the_process(tmp_path, monkeypatch, "[usage]\ngo_month_limit = 5.0\n")
    assert set(env.values()) == {f"http://inherited-{v}:9" for v in
                                 ("https_proxy", "http_proxy", "all_proxy", "no_proxy")}
    assert len(env) == 8  # both letter cases, as they were


_INHERITED = {"HTTPS_PROXY": "http://hub:1", "https_proxy": "http://hub:1",
              "HTTP_PROXY": "http://hub:1", "http_proxy": "http://hub:1",
              "ALL_PROXY": "http://hub:1", "all_proxy": "http://hub:1",
              "NO_PROXY": "hub.local", "no_proxy": "hub.local", "PATH": "/bin"}
_URL_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
_OWN_URL = {v: "http://127.0.0.1:8080" for v in _URL_VARS}


def _inherited_without_no_proxy() -> dict[str, str]:
    return {k: v for k, v in _INHERITED.items() if k.lower() != "no_proxy"}


@pytest.mark.parametrize("section, want", [
    # nothing configured — the whole hub environment is inherited
    ('[usage]\n', _INHERITED),
    ('[providers.opencode]\n', _INHERITED),
    # proxy only — the URL variables are replaced, the inherited NO_PROXY stays
    ('[providers.opencode]\nproxy = "http://127.0.0.1:8080"\n', {**_INHERITED, **_OWN_URL}),
    # proxy = "" — no proxy at all, the inherited NO_PROXY stays
    ('[providers.opencode]\nproxy = ""\n',
     {"PATH": "/bin", "NO_PROXY": "hub.local", "no_proxy": "hub.local"}),
    # no_proxy only — the proxy URL variables stay, NO_PROXY is replaced
    ('[providers.opencode]\nno_proxy = "localhost"\n',
     {**_inherited_without_no_proxy(), "NO_PROXY": "localhost", "no_proxy": "localhost"}),
    # no_proxy = "" — no bypass list, the proxy URL variables stay
    ('[providers.opencode]\nno_proxy = ""\n', _inherited_without_no_proxy()),
    # both keys — each one on its own
    ('[providers.opencode]\nproxy = "http://127.0.0.1:8080"\nno_proxy = "localhost"\n',
     {**_OWN_URL, "NO_PROXY": "localhost", "no_proxy": "localhost", "PATH": "/bin"}),
    ('[providers.opencode]\nproxy = "http://127.0.0.1:8080"\nno_proxy = ""\n',
     {**_OWN_URL, "PATH": "/bin"}),
    ('[providers.opencode]\nproxy = ""\nno_proxy = "localhost"\n',
     {"PATH": "/bin", "NO_PROXY": "localhost", "no_proxy": "localhost"}),
    ('[providers.opencode]\nproxy = ""\nno_proxy = ""\n', {"PATH": "/bin"}),
])
def test_provider_proxy_keys_independent(tmp_path, section, want):
    """[providers.<name>] per key: absent — inherit, "" — explicitly none, a value — set."""
    from ahub import config, prepare

    write(paths.global_config_path(), section)
    p = config.load_hub().provider("opencode")
    assert prepare.apply_proxy(dict(_INHERITED), p.proxy, p.no_proxy) == want
    assert _INHERITED["HTTPS_PROXY"] == "http://hub:1"  # the input is not touched


def _sleepers(marker: str) -> list[int]:
    """Live processes with a marker in the cmdline — via procs (psutil on macOS, no /proc)."""
    from ahub import procs
    return [pid for pid in procs.pids() if procs.alive(pid) and any(marker in a for a in procs.cmdline(pid))]


@pytest.mark.parametrize("detach", [False, True])
def test_leftover_processes_reaped_after_normal_exit(fake, tmp_path, detach):
    secs = 97.123 if detach else 96.321  # marker in the cmdline of an abandoned process
    r = run(fake, spec(tmp_path, {"session": "s", "steps": [
        {"bg": secs, "detach": detach}, {"event": {"type": "text", "text": "готово"}}]}, idle_s=0))
    assert r.ok
    time.sleep(1.0)
    assert _sleepers(f"time.sleep({secs})") == [], "брошенный агентом процесс пережил ход"
