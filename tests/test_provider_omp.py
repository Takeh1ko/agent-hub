"""omp contract against live JSONL samples and a local executable; no network or real HOME."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from ahub import catalog, cli, config, doctor, paths, providers, quota, registry
from ahub.config import QuotaConfig
from ahub.model import Role
from ahub.providers.base import Act, Cap, CatalogEntry, Outcome, PlanKind, RunSpec
from ahub.providers.omp import THINKING_LEVELS, OmpProvider, parse_models, usage_of
from ahub.providers.runner import run
from ahub.store import Store
from ahub.transcript import Reader, tool_icon
from tests.conftest import write

DATA = Path(__file__).parent / "data" / "omp"
MODEL = "opencode-go/deepseek-v4.1-flash"
SECRET = "omp-test-key-never-display"


@pytest.fixture
def omp():
    return OmpProvider(binary="/missing/omp")


def acts_of(provider, name, now=1000):
    return [a for line in (DATA / name).read_text().splitlines() for a in provider.parse_line(line, now)]


def classify(provider, acts, code=0):
    sid = next((a.text for a in acts if a.kind is Act.SESSION), None)
    return provider.classify(exit_code=code, activities=acts, session_id=sid, stderr_tail="")


def test_success_with_tool_and_usage(omp):
    acts = acts_of(omp, "tool_read_ok.jsonl")
    assert acts[0].kind is Act.SESSION
    assert classify(omp, acts)[0] is Outcome.OK
    assert omp.final_text(acts) == "hello"
    assert [a.text for a in acts if a.kind is Act.TEXT] == ["Прочитаю файл.\n", "hello"]
    tools = [a for a in acts if a.kind in (Act.TOOL_START, Act.TOOL_END)]
    assert [(a.kind, a.tool) for a in tools] == [(Act.TOOL_START, "read"), (Act.TOOL_END, "read")]
    assert tools[0].data["input"] == {"path": "a.txt"}
    assert "hello" in tools[1].data["output"]
    assert tools[1].data["status"] == "completed"
    u = omp.stream_usage(acts)
    assert (u.tokens_in, u.tokens_out, u.cache_read, u.context) == (20123, 43, 13583, 16882)
    assert u.cost_usd is None and u.cost_go is None  # zero prices are unknown, not free
    assert len([a for a in acts if a.kind is Act.USAGE]) == 2  # repeated snapshots aren't billed


def test_live_resume_samples(omp):
    start, resume = (acts_of(omp, n) for n in ("start.jsonl", "resume.jsonl"))
    assert start[0].text == resume[0].text
    assert omp.final_text(start) == "PONG"
    assert omp.final_text(resume) == "OTTER"  # remembers the previous prompt
    assert classify(omp, resume)[0] is Outcome.OK
    u = omp.stream_usage(resume)
    assert (u.tokens_in, u.tokens_out, u.tokens_reasoning, u.cache_read) == (2340, 23, 15, 9021)


def test_quota_reset_shared_with_scheduler(omp, monkeypatch):
    now = 1_900_000_000_000
    monkeypatch.setattr("ahub.providers.omp.now_ms", lambda: now)
    acts = acts_of(omp, "go_quota_429.jsonl", now)
    assert classify(omp, acts, 1)[0] is Outcome.QUOTA
    reset = now + 36060000
    assert all(a.data["reset_at"] == reset for a in acts if a.kind is Act.ERROR)
    # A separate service/provider instance sees the same hold, and only Go models match it.
    other = OmpProvider(binary="/missing/omp")
    buckets = other.quota(force=True)
    assert len(buckets) == 1
    b = buckets[0]
    assert (b.remaining, b.reset_at) == (0, reset)
    assert b.models(MODEL) and b.models("opencode-go/muse-spark-1.3-contributor")
    assert not b.models("willow/gpt-6.1-sol")
    assert quota.breached(buckets, QuotaConfig(), now) is b
    reason, _event = quota.describe_error("worker", buckets, "429")
    assert json.loads(reason)["code"] == "wait_quota"
    monkeypatch.setattr("ahub.providers.omp.now_ms", lambda: reset)
    assert other.quota() == []


@pytest.mark.parametrize("text,status,outcome", [
    ("fetch failed: connection reset", None, Outcome.TRANSIENT),
    ("upstream unavailable", 503, Outcome.TRANSIENT),
    ("invalid model", 400, Outcome.MODEL_ERROR),
    ("invalid api key", 401, Outcome.NO_ACCESS),
    ("429 Too Many Requests", 429, Outcome.QUOTA),
])
def test_error_outcomes_even_at_exit_zero(omp, text, status, outcome):
    acts = omp.parse_line(json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "error", "errorStatus": status, "errorMessage": text}}), 1)
    assert omp.classify(exit_code=0, activities=acts, session_id="id", stderr_tail="")[0] is outcome


def test_stderr_errors_and_recovered_retry(omp):
    assert omp.classify(exit_code=1, activities=[], session_id="id", stderr_tail="fetch failed")[0] \
        is Outcome.TRANSIENT
    error = {"type": "message_end", "message": {"role": "assistant", "stopReason": "error",
             "errorStatus": 429, "errorMessage": "Go usage limit exceeded retry-after-ms=1000"}}
    acts = omp.parse_line(json.dumps(error), 100)
    acts += acts_of(omp, "start.jsonl")
    assert classify(omp, acts)[0] is Outcome.OK
    assert omp.quota() == []  # an error recovered by omp must not hold future work


@pytest.mark.parametrize("raw", ["", "noise", "{", "[]", "null", '{"type":"message_end","message":[]}',
                                    '{"type":"message_end","message":{"role":"assistant","content":[null,1]}}'])
def test_parse_noise_never_raises(omp, raw):
    assert isinstance(omp.parse_line(raw, 1), list)


def test_reasoning_tool_results_and_icons(omp):
    acts = omp.parse_line(json.dumps({"type": "message_end", "message": {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "checking"}, {"type": "text", "text": "done"}]}}), 1)
    assert [(a.kind, a.text) for a in acts[:2]] == [(Act.TEXT, "done"), (Act.REASONING, "checking")]
    for tool, icon in (("bash", "▶"), ("edit", "✎"), ("write", "✎"), ("read", "👁"), ("grep", "🔎")):
        act = omp.parse_line(json.dumps({"type": "tool_execution_start", "toolName": tool, "args": {}}), 1)[0]
        assert tool_icon(act.tool).startswith(icon)
    failed = omp.parse_line(json.dumps({"type": "tool_execution_end", "toolName": "bash", "isError": True,
                           "result": {"content": [{"type": "text", "text": "x" * 1000}]}}), 1)
    assert failed[0].kind is Act.TOOL_END and failed[0].data["status"] == "error"
    assert len(failed[0].data["output"]) == 300
    assert omp.classify(exit_code=0, activities=failed, session_id="id", stderr_tail="")[0] is Outcome.OK


def test_build_start_resume_and_isolated_session_dir(omp, tmp_path):
    spec = RunSpec(prompt="--prompt", cwd=str(tmp_path), model_id=MODEL, variant="max")
    cmd = omp.build_command(spec)
    assert cmd[:6] == ["/missing/omp", "-p", "--mode", "json", "--model", MODEL]
    assert cmd[cmd.index("--thinking") + 1] == "max"
    assert cmd[cmd.index("--approval-mode") + 1] == "yolo"
    assert cmd[cmd.index("--cwd") + 1] == spec.cwd
    sessions = cmd[cmd.index("--session-dir") + 1]
    assert Path(sessions).is_relative_to(paths.state_dir()) and Path(sessions).is_dir()
    assert "--resume" not in cmd and "--no-session" not in cmd
    assert cmd[-2:] == ["--", "--prompt"]
    spec.session_id = "same-id"
    resumed = omp.build_command(spec)
    assert resumed[resumed.index("--resume") + 1] == "same-id"
    assert resumed[resumed.index("--session-dir") + 1] == sessions
    other = omp.build_command(RunSpec("hello", str(tmp_path / "other"), MODEL))
    assert other[other.index("--session-dir") + 1] != sessions


def test_config_env_key_and_missing_file(tmp_path):
    key = write(tmp_path / "go-key", SECRET + "\n")
    write(paths.global_config_path(), f'[providers.omp]\nenabled = false\nbinary = "~/bin/omp-test"\n'
          f'api_key_file = "{key}"\nproxy = ""\n')
    settings = config.load_hub().provider("omp")
    assert not settings.enabled and settings.proxy == ""
    assert settings.binary == "~/bin/omp-test" and settings.api_key_file == str(key)
    prov = OmpProvider()
    assert prov._bin() == str(Path.home() / "bin/omp-test")
    spec = RunSpec("hello", str(tmp_path), MODEL, env={"EXTRA": "1"})
    env = prov.env(spec)
    assert env["OPENCODE_API_KEY"] == SECRET and env["EXTRA"] == "1"
    assert env["AHUB_HOME"] == str(tmp_path / ".ahub/home")
    assert "HOME" not in env and SECRET not in repr(prov.build_command(spec))
    key.unlink()
    assert "OPENCODE_API_KEY" not in prov.env(spec)


@pytest.fixture
def fake_omp(tmp_path):
    binary = tmp_path / "bin" / "omp"
    write(binary, f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
data = Path(os.environ["OMP_TEST_DATA"])
if "--version" in args:
    print("omp v18.14.0")
elif "models" in args:
    print((data / "models.json").read_text())
else:
    assert args[args.index("--approval-mode") + 1] == "yolo"
    assert os.environ["OPENCODE_API_KEY"] == "omp-test-key-never-display"
    name = "go_quota_429.jsonl" if args[-1] == "quota" else ("resume.jsonl" if "--resume" in args else "start.jsonl")
    print((data / name).read_text(), end="")
    sys.exit(1 if name == "go_quota_429.jsonl" else 0)
''')
    binary.chmod(0o755)
    key = write(tmp_path / "key", SECRET)
    return OmpProvider(binary=str(binary), api_key_file=str(key), env={"OMP_TEST_DATA": str(DATA)})


def test_runner_start_resume_quota_and_transcript(fake_omp, tmp_path, monkeypatch):
    spec = RunSpec("hello", str(tmp_path), MODEL, log_path=str(tmp_path / "omp.log"), timeout_s=10)
    first = run(fake_omp, spec)
    assert first.outcome is Outcome.OK and first.final_text == "PONG" and first.session_id
    spec.session_id = first.session_id
    spec.prompt = "remember"
    second = run(fake_omp, spec)
    assert second.outcome is Outcome.OK and second.session_id == first.session_id and second.final_text == "OTTER"
    assert second.usage.tokens_in == 2340  # previous run's usage is not counted
    monkeypatch.setitem(providers._cache, "omp", fake_omp)
    chunk = Reader(fake_omp, spec.log_path).read()
    assert [i.text for i in chunk.items if i.kind == "text"] == ["PONG", "OTTER"]
    assert SECRET not in repr(chunk)
    assert SECRET not in Path(spec.log_path).read_text()
    spec.prompt = "quota"
    third = run(fake_omp, spec)
    assert third.outcome is Outcome.QUOTA and fake_omp.quota()


def test_health_catalog_doctor_no_secrets(fake_omp, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(providers._cache, "omp", fake_omp)
    h = fake_omp.health()
    assert h.ok and h.details["version"] == "omp v18.14.0" and h.details["key_present"]
    models = fake_omp.catalog()
    go = next(m for m in models if m.model_id == MODEL)
    willow = next(m for m in models if m.model_id == "willow/gpt-6.1-sol")
    assert go.counter == "quota" and go.variants == THINKING_LEVELS
    assert go.price_in is None and willow.price_in is None and willow.counter == "usd"
    assert doctor.check_omp().ok is True
    state = doctor.provider_state("omp", auth=[])
    assert state.found and state.logged_in
    from ahub.commands.doctor import text

    assert SECRET not in text([doctor.check_omp()])
    assert SECRET not in json.dumps(asdict(h)) + repr(models) + capsys.readouterr().out
    Path(fake_omp.api_key_file).unlink()
    assert fake_omp.health().ok and not fake_omp.health().details["key_present"]  # omp lists its own models
    assert "OpenCode Go" in doctor.check_omp().detail and doctor.check_omp().ok is True
    empty = write(tmp_path / "no-models" / "models.json", '{"models":[]}').parent
    fake_omp.extra_env["OMP_TEST_DATA"] = str(empty)
    assert not fake_omp.health().ok and doctor.check_omp().ok is False
    fake_omp.binary = "/missing/omp"
    assert doctor.check_omp().ok is None


def test_key_redacted_from_activities_and_version(fake_omp, monkeypatch):
    line = json.dumps({"type": "tool_execution_start", "toolName": "bash", "args": {"command": SECRET}})
    assert SECRET not in repr(fake_omp.parse_line(line, 1))
    monkeypatch.setattr("ahub.providers.omp.run_capture", lambda *a, **kw: (0, f"omp v18.14.0 {SECRET}", ""))
    assert SECRET not in repr(fake_omp.health())
    outcome, err = fake_omp.classify(exit_code=1, activities=[], session_id="id", stderr_tail=f"error: {SECRET}")
    assert outcome is Outcome.MODEL_ERROR and SECRET not in err


def test_broken_binary_health_and_absent_capabilities(tmp_path):
    binary = write(tmp_path / "omp", "#!/bin/sh\nexit 1\n")
    binary.chmod(0o755)
    prov = OmpProvider(binary=str(binary))
    assert not prov.health().ok and len(prov.health().problems) == 2
    assert not prov.has(Cap.EXPORT) and not prov.has(Cap.FIND_SESSION) and not prov.has(Cap.STRUCTURED)
    assert prov.find_session(str(tmp_path), 0) is None and prov.export("id") is None


def test_money_and_catalog_prices():
    u = usage_of({"input": 10, "output": 2, "cacheRead": 8, "cacheWrite": 1, "cost": {"total": 0.03}})
    assert u.cost_usd == 0.03 and u.context == 19
    assert usage_of(None) is None and usage_of({"input": "10"}) is None
    assert parse_models("broken") == [] and parse_models('{"models":null}') == []
    models = parse_models(json.dumps({"models": [{"provider": "willow", "id": "gpt-test", "reasoning": True,
                                               "cost": {"input": 1.0, "output": 2.0}}]}))
    assert (models[0].price_in, models[0].price_out) == (1.0, 2.0)


VIBE = "mistral-vibe/deepseek-v4-1-flash-1017"
VIBE_MODELS = json.dumps({"models": [
    {"provider": "mistral-vibe", "kind": "chat", "id": "deepseek-v4-1-flash-1017", "name": "DeepSeek V4.1 Flash 1017",
     "contextWindow": 1048576, "reasoning": True, "cost": {"input": 0, "output": 0}, "pricingStatus": "unknown"},
    {"provider": "mistral-vibe", "kind": "embedding", "id": "codestral-embed"},
    {"provider": "someone", "kind": "chat", "id": "paid", "cost": {"input": 3.0, "output": 9.0}},
]})


def test_any_configured_provider_in_catalog_vibe_is_a_subscription():
    vibe, other = parse_models(VIBE_MODELS)
    assert (vibe.model_id, vibe.plan, vibe.price_in, vibe.context) == (VIBE, PlanKind.SUBSCRIPTION, None, 1048576)
    assert vibe.variants == THINKING_LEVELS and vibe.counter == "quota"
    assert (other.model_id, other.plan, other.price_in) == ("someone/paid", PlanKind.PAYG, 3.0)
    entry = registry.ModelEntry("vibe", "omp", VIBE, "high")
    assert registry.plan_kind(entry) is PlanKind.SUBSCRIPTION
    assert registry.plan_kind(entry, vibe) is PlanKind.SUBSCRIPTION
    assert registry.cost_kind(entry) == "plan"


def test_no_go_key_no_go_fallback_when_omp_has_its_own_models(tmp_path, monkeypatch):
    prov = OmpProvider(binary="/missing/omp", api_key_file=str(tmp_path / "absent"))
    monkeypatch.setattr("ahub.providers.omp.run_capture", lambda *a, **kw: (0, VIBE_MODELS, ""))
    ids = [m.model_id for m in prov.catalog()]
    assert VIBE in ids and MODEL not in ids
    prov.api_key_file = str(write(tmp_path / "key", SECRET))
    assert MODEL in [m.model_id for m in prov.catalog()]  # with the key both work


def test_vibe_429_is_quota_without_touching_the_go_bucket(omp):
    line = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": "error", "errorStatus": 429,
        "errorMessage": "429 rate limit, retry-after-ms: 60000"}})
    acts = omp.parse_line(line, 1000)
    assert omp.classify(exit_code=1, activities=acts, session_id="s", stderr_tail="")[0] is Outcome.QUOTA
    assert omp.quota() == []


def test_catalog_does_not_borrow_opencode_prices_or_levels(omp):
    opencode = CatalogEntry(MODEL, plan=PlanKind.GO, price_in=0.15, reasoning=("low", "high"))
    own = next(m for m in omp.catalog() if m.model_id == MODEL)
    index = catalog.index_by_id({"opencode": [opencode], "omp": [own]})
    old = registry.ModelEntry("whale", "opencode", MODEL)
    new = registry.ModelEntry("whale-omp", "omp", MODEL)
    assert catalog.match_entry(old, index) is opencode
    assert catalog.match_entry(new, index) is own
    assert tuple(registry.valid_levels(new, catalog.match_entry(new, index), index)) == THINKING_LEVELS
    assert catalog.reasoning_text(new, own, index) == "off–max"
    assert registry.plan_kind(new, own) is PlanKind.SUBSCRIPTION and own.price_in is None
    assert catalog.match_entry(new, catalog.index_by_id({"opencode": [opencode]})) is None


def test_default_key_path_and_bad_key_encoding(tmp_path):
    prov = OmpProvider(binary="/missing/omp")
    spec = RunSpec("hello", str(tmp_path), MODEL)
    assert "OPENCODE_API_KEY" not in prov.env(spec)
    key = write(Path.home() / ".config/opencode-go/api_key", SECRET + "\n")
    assert prov.env(spec)["OPENCODE_API_KEY"] == SECRET
    key.write_bytes(b"\xff")
    assert "OPENCODE_API_KEY" not in prov.env(spec)


def test_provider_proxy_applied_by_runner(fake_omp, tmp_path, monkeypatch):
    from ahub.providers import runner

    write(paths.global_config_path(), '[providers.omp]\nproxy = "http://127.0.0.1:5555"\nno_proxy = "localhost"\n')
    real = runner.subprocess.Popen
    seen = []

    def capture(cmd, **kwargs):
        seen.append(kwargs["env"])
        return real(cmd, **kwargs)

    monkeypatch.setattr(runner.subprocess, "Popen", capture)
    result = run(fake_omp, RunSpec("hello", str(tmp_path), MODEL, timeout_s=10))
    assert result.ok
    assert seen[0]["HTTPS_PROXY"] == "http://127.0.0.1:5555" and seen[0]["NO_PROXY"] == "localhost"
    assert seen[0]["OPENCODE_API_KEY"] == SECRET


def test_alias_commands_in_fake_home_and_plan(omp, monkeypatch):
    monkeypatch.setitem(providers._cache, "omp", omp)
    monkeypatch.setattr("ahub.catalog.get_catalogs", lambda **kw: {"omp": omp.catalog()})
    for alias, mid, variant in (("whale-omp", MODEL, "max"),
                               ("spark-omp", "opencode-go/muse-spark-1.3-contributor", "xhigh")):
        assert cli.main(["models", "add", alias, "--provider", "omp", "--model-id", mid, "--variant", variant]) == 0
        entry = registry.get(Store(), alias)
        assert (entry.provider, entry.model_id, entry.variant) == ("omp", mid, variant)
        assert registry.cost_kind(entry) == "plan"
        assert set(registry.valid_levels(entry)) == set(THINKING_LEVELS)
    assert "omp" in providers.names() and "omp" in registry.PLAN_PROVIDERS
    # The omp key check owns its credentials; no dependency on opencode's auth.json.
    for role in Role:
        registry.add_to_role(Store(), role, "whale-omp", default=True)
    assert doctor.check_models(auth=[]).ok is True
