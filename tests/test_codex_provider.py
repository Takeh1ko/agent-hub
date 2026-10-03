"""codex provider: parsing saved live samples, error classification, command, catalog,
health, contract on a fake executable. Live runs are marked live."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ahub.providers.base import Act, Cap, Outcome, RunSpec
from ahub.providers.codex import (
    CodexProvider,
    classify_text,
    error_text,
    parse_models,
    prompt_arg,
    sandbox_mode,
    usage_of,
)

DATA = Path(__file__).parent / "data" / "codex"
SID = "01a0fec9-9da7-7c71-9fa0-cdba1d4bfc35"


@pytest.fixture
def codex() -> CodexProvider:
    return CodexProvider(binary="/usr/bin/codex-fake")


def acts_of(provider: CodexProvider, sample: str) -> list:
    out = []
    for line in (DATA / sample).read_text(encoding="utf-8").splitlines():
        out.extend(provider.parse_line(line, 1))
    return out


def outcome_of(provider: CodexProvider, sample: str, rc: int, stderr: str = "", sid: str = SID):
    acts = acts_of(provider, sample)
    return provider.classify(exit_code=rc, activities=acts, session_id=sid, stderr_tail=stderr)[0], acts


def test_parse_hello_sample(codex):
    acts = acts_of(codex, "hello.ndjson")
    assert acts[0].kind is Act.SESSION and acts[0].text == SID
    assert [a.kind for a in acts] == [Act.SESSION, Act.STEP, Act.TEXT, Act.STEP, Act.USAGE]
    assert codex.final_text(acts) == "OK"
    u = codex.stream_usage(acts)
    assert (u.tokens_in, u.tokens_out, u.cache_read, u.context) == (11779, 5, 9984, 11779)
    assert u.cost_usd is None and u.cost_go is None and u.quota is None  # a subscription, no prices


def test_parse_commands_sample(codex):
    acts = acts_of(codex, "commands.ndjson")
    tools = [a for a in acts if a.kind in (Act.TOOL_START, Act.TOOL_END)]
    assert [(a.kind, a.tool) for a in tools] == [(Act.TOOL_START, "command_execution"),
                                                 (Act.TOOL_END, "command_execution")]
    assert tools[0].data["input"]["command"] == "/bin/bash -lc 'echo hello > sbox.txt'"
    assert tools[1].data["exit_code"] == 0 and tools[1].data["status"] == "completed"
    assert codex.final_text(acts) == "DONE"


def test_failed_command_is_not_a_turn_error(codex):
    """A failing test command inside the copy must not fail the turn — it is a tool result."""
    item = {"id": "item_1", "type": "command_execution", "command": "/bin/bash -lc 'pytest -q'",
            "aggregated_output": "3 failed", "exit_code": 1, "status": "failed"}
    acts = codex.parse_line(json.dumps({"type": "item.completed", "item": item}), 1)
    assert [a.kind for a in acts] == [Act.TOOL_END]
    assert acts[0].data["exit_code"] == 1 and acts[0].data["status"] == "failed"
    out, _err = codex.classify(exit_code=0, activities=acts, session_id=SID, stderr_tail="")
    assert out is Outcome.OK


def test_parse_resume_sample(codex):
    acts = acts_of(codex, "resume.ndjson")
    assert acts[0].kind is Act.SESSION and acts[0].text == SID  # the same thread
    assert codex.final_text(acts) == "PONG2"
    assert codex.stream_usage(acts).tokens_out == 7


def test_parse_structured_sample(codex):
    acts = acts_of(codex, "structured.ndjson")
    assert codex.structured(codex.final_text(acts), acts, {"type": "object"}) == {"answer": "OK"}
    assert codex.structured("просто текст", [], None) is None


def test_noise_and_broken_lines(codex):
    assert codex.parse_line("Reading additional input from stdin...", 1) == []
    assert codex.parse_line('{"type":"thread.started"', 1) == []
    assert codex.parse_line("[1, 2]", 1) == []
    assert [a.kind for a in codex.parse_line('{"type":"thread.archived"}', 1)] == [Act.OTHER]


def test_outcomes_of_real_samples(codex):
    assert outcome_of(codex, "hello.ndjson", 0)[0] is Outcome.OK
    assert outcome_of(codex, "commands.ndjson", 0)[0] is Outcome.OK
    assert outcome_of(codex, "structured.ndjson", 0)[0] is Outcome.OK
    assert outcome_of(codex, "resume.ndjson", 0)[0] is Outcome.OK
    # 400: the model is not in the plan — no access to the model, a retry would not help
    assert outcome_of(codex, "model_error.ndjson", 1)[0] is Outcome.NO_ACCESS
    # 401 without a login
    assert outcome_of(codex, "no_access.ndjson", 1)[0] is Outcome.NO_ACCESS
    # reconnect spam on a dead network
    assert outcome_of(codex, "network_error.ndjson", 1)[0] is Outcome.TRANSIENT


def test_model_error_text(codex):
    _out, acts = outcome_of(codex, "model_error.ndjson", 1)
    text = codex.classify(exit_code=1, activities=acts, session_id=SID, stderr_tail="")[1]
    assert "status 400" in text and "not supported when using Codex with a ChatGPT account" in text


def test_unknown_session_still_ok(codex):
    out, _ = codex.classify(exit_code=0, activities=acts_of(codex, "hello.ndjson"), session_id="new",
                           stderr_tail="Error: thread/resume failed: no rollout found for thread id x\n")
    assert out is Outcome.OK  # codex opened a new session — the turn happened


@pytest.mark.parametrize("text,flag", [
    ("stream disconnected before completion: Connection refused (os error 111)", "transient"),
    ("error sending request for url (https://api.openai.com/v1/responses)", "transient"),
    ("unexpected status 500 from the model endpoint", "transient"),
    ("Reconnecting... 2/5 (unexpected status 429 Too Many Requests)", "quota"),
    ("You've hit your usage limit for this week", "quota"),
    ('{"type":"error","status":429,"error":{"type":"usage_limit_reached"}}', "quota"),
    ("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header", "no_access"),
    ("Not logged in. Run codex login", "no_access"),
    ("The 'gpt-4o' model is not supported when using Codex with a ChatGPT account.", "no_access"),
    ("unexpected status 400 invalid_request_error", None),
    ("the response was cut short", None),
    ("request id: req_8ff2551c044e47f5a675b8a96a0e96ab", None),  # ids must not look like a status
])
def test_classify_forms(text, flag):
    flags = classify_text(text)
    for f in ("transient", "quota", "no_access"):
        assert flags[f] is (f == flag), (text, flags)


def test_error_text_unwraps_json_body():
    text, flags = error_text({"message": '{"type":"error","status":503,"error":'
                                     '{"type":"server_error","message":"upstream is down"}}'})
    assert text == "status 503: upstream is down" and flags["transient"] is True
    text, flags = error_text("plain text")
    assert text == "plain text" and not any(flags[k] for k in ("transient", "quota", "no_access"))
    assert error_text(None) == ("", {"transient": False, "quota": False, "no_access": False, "status": None})


def test_usage_of_shapes():
    assert usage_of(None) is None and usage_of({"output_tokens": "x"}) is None
    u = usage_of({"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 8,
                  "cache_write_input_tokens": 0, "reasoning_output_tokens": 1})
    assert (u.tokens_in, u.tokens_out, u.cache_read, u.tokens_reasoning, u.context) == (10, 2, 8, 1, 10)


def test_build_command(codex, tmp_path):
    spec = RunSpec(prompt="привет", cwd=str(tmp_path), model_id="gpt-5.6-terra", variant="high",
                   timeout_s=1234)
    cmd = codex.build_command(spec)
    assert cmd[:5] == ["/usr/bin/codex-fake", "exec", "--json", "-m", "gpt-5.6-terra"]
    assert 'approval_policy="never"' in cmd  # no waiting for a human
    assert cmd[cmd.index("-s") + 1] == "workspace-write"  # the OS sandbox
    assert cmd[cmd.index("-C") + 1] == str(tmp_path)
    assert 'model_reasoning_effort="high"' in cmd
    assert cmd[-1] == "привет"
    assert "--skip-git-repo-check" in cmd  # tmp_path is not a git repo
    (tmp_path / ".git").mkdir()
    assert "--skip-git-repo-check" not in codex.build_command(spec)  # a worktree has .git too

    spec.session_id = SID
    rcmd = codex.build_command(spec)
    assert rcmd[:4] == ["/usr/bin/codex-fake", "exec", "resume", "--json"]
    assert 'sandbox_mode="workspace-write"' in rcmd  # `exec resume` has no -s/-C
    assert "-s" not in rcmd and "-C" not in rcmd
    assert "--skip-git-repo-check" not in rcmd
    assert rcmd[rcmd.index(SID) + 1] == "привет"  # session id then prompt
    (tmp_path / ".git").rmdir()
    # outside a repo codex stops to ask about the trust — on resume too (checked live 2026-10-03)
    assert "--skip-git-repo-check" in codex.build_command(spec)

    spec.schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    scmd = codex.build_command(spec)
    schema_path = scmd[scmd.index("--output-schema") + 1]
    assert json.loads(Path(schema_path).read_text()) == spec.schema
    assert schema_path.startswith(str(tmp_path / ".ahub"))


def test_build_command_without_sandbox_or_approvals(tmp_path):
    """sandbox="" — the user's config decides; approvals="" — as is."""
    bare = CodexProvider(binary="/usr/bin/codex-fake", sandbox="", approvals="")
    cmd = bare.build_command(RunSpec(prompt="hi", cwd=str(tmp_path), model_id="m"))
    assert "-s" not in cmd and "-c" not in cmd


def test_sandbox_mode_default_config_and_explicit(tmp_path, monkeypatch):
    """Absent key — workspace-write; the configured mode goes to exec (-s) and resume (-c sandbox_mode=)."""
    from ahub import paths
    from tests.conftest import write

    assert sandbox_mode() == "workspace-write"  # no config — the default keeps the isolation
    cfg = paths.global_config_path()
    write(cfg, '[providers.codex]\nsandbox = "danger-full-access"\n')
    assert sandbox_mode() == "danger-full-access"
    assert CodexProvider(binary="/usr/bin/codex-fake").sandbox == "danger-full-access"
    assert CodexProvider(binary="/usr/bin/codex-fake", sandbox="read-only").sandbox == "read-only"
    assert CodexProvider(binary="/usr/bin/codex-fake", sandbox="").sandbox == ""

    prov = CodexProvider(binary="/usr/bin/codex-fake")
    spec = RunSpec(prompt="hi", cwd=str(tmp_path), model_id="m")
    cmd = prov.build_command(spec)
    assert cmd[cmd.index("-s") + 1] == "danger-full-access"
    spec.session_id = SID
    rcmd = prov.build_command(spec)
    assert 'sandbox_mode="danger-full-access"' in rcmd and "-s" not in rcmd


def test_sandbox_mode_survives_a_broken_config(monkeypatch):
    """A config that does not parse must not stop the provider — the default isolation stands."""
    from ahub import config, paths
    from tests.conftest import write

    write(paths.global_config_path(), "providers = 5\n")
    with pytest.raises(config.ConfigError):
        config.load_hub()
    assert sandbox_mode() == "workspace-write"


def test_danger_full_access_is_not_probed(tmp_path, monkeypatch):
    """No OS sandbox — the probe is skipped, so health() cannot report it as a failure."""
    from ahub.providers import codex as codex_mod

    fake = _fake_codex(tmp_path)
    env = {"AHUB_CODEX_FAKE_DATA": str(DATA)}
    real = codex_mod.run_capture

    def no_probe(cmd, **kw):
        assert "sandbox" not in cmd, "the probe must not run when there is no OS sandbox"
        return real(cmd, **kw)

    monkeypatch.setattr(codex_mod, "run_capture", no_probe)
    prov = CodexProvider(binary=str(fake), env=env, sandbox="danger-full-access")
    assert prov.sandbox_ok() == (True, "")
    h = prov.health()
    assert h.ok and h.details["sandbox"] == "danger-full-access" and h.details["sandbox_ok"] is True


def test_env_isolates_the_hub(codex, tmp_path):
    env = codex.env(RunSpec(prompt="hi", cwd=str(tmp_path), model_id="m", env={"X": "1"}))
    assert env["AHUB_HOME"] == str(tmp_path / ".ahub" / "home") and env["X"] == "1"
    assert "HOME" not in env  # codex needs the real one for the login


def test_capabilities_and_missing_binary(codex):
    assert codex.has(Cap.RESUME) and codex.has(Cap.STREAM) and codex.has(Cap.STRUCTURED)
    assert codex.has(Cap.TOKENS) and codex.has(Cap.CATALOG) and codex.has(Cap.HEALTH)
    assert not codex.has(Cap.COST_MONEY) and not codex.has(Cap.COST_QUOTA)  # a subscription
    assert not codex.has(Cap.EXPORT) and not codex.has(Cap.FIND_SESSION)
    h = codex.health()
    assert not h.ok and "codex-fake" in h.problems[0]


def test_parse_models():
    models = parse_models((DATA / "models.json").read_text(encoding="utf-8"))
    assert [m.model_id for m in models] == ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5"]
    assert models[0].note == "GPT-5.6-Terra" and models[0].counter == "quota"
    assert models[0].variants == ("low", "medium", "high", "xhigh", "max", "ultra")  # reasoning levels
    assert parse_models("") == [] and parse_models('{"models": "no"}') == []


def _fake_codex(tmp_path: Path) -> Path:
    from tests.provider_contract import fake_codex

    return fake_codex(tmp_path)


def test_contract_on_fake_binary(tmp_path):
    """The shared contract set on a fake executable: no network, answers are live samples."""
    from tests import provider_contract as contract

    fake = _fake_codex(tmp_path)
    env = {"AHUB_CODEX_FAKE_DATA": str(DATA)}
    prov = CodexProvider(binary=str(fake), env=env)

    def make(kind: str, cwd: str) -> RunSpec:
        word = "PONG" if kind == "hello" else "PONG2"
        return RunSpec(prompt=f"Ответь одним словом: {word}", cwd=cwd, model_id="gpt-5.6-luna",
                       log_path=str(tmp_path / f"{kind}.log"), timeout_s=60, idle_s=30, env=env)

    contract.check_catalog_and_health(prov)
    contract.check_session_cycle(prov, make, str(tmp_path))


def test_health_reports_login_models_and_sandbox(tmp_path):
    fake = _fake_codex(tmp_path)
    prov = CodexProvider(binary=str(fake), env={"AHUB_CODEX_FAKE_DATA": str(DATA)})
    h = prov.health()
    assert h.ok and h.details["models"] == 3 and h.details["login"].startswith("Logged in")
    assert h.details["sandbox"] == "workspace-write" and h.details["sandbox_ok"] is True
    assert h.details["version"].startswith("codex-cli")

    # not logged in + a sandbox that cannot start — both visible, with the reason
    class _NoLogin(CodexProvider):
        def login(self):
            return False, "Not logged in"

        def sandbox_ok(self):
            return False, "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"

    h2 = _NoLogin(binary=str(fake), env={"AHUB_CODEX_FAKE_DATA": str(DATA)}).health()
    assert not h2.ok and len(h2.problems) == 2
    assert "codex login" in h2.problems[0] and "bwrap" in h2.problems[1]
    assert h2.details["sandbox_ok"] is False  # the doctor shows the fix for exactly this


def test_health_survives_a_broken_binary(tmp_path):
    broken = tmp_path / "broken" / "codex"
    broken.parent.mkdir(parents=True)
    broken.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")  # answers nothing, like a stub
    broken.chmod(0o755)
    h = CodexProvider(binary=str(broken)).health()
    assert not h.ok and h.details.get("models", 0) == 0
    assert any("codex login" in p for p in h.problems) and any("models" in p for p in h.problems)


def test_sandbox_broken_reason_is_read_from_stderr(tmp_path, monkeypatch):
    """`codex sandbox` writes the reason to stderr; rc != 0 means commands would fail silently."""
    fake = _fake_codex(tmp_path)
    prov = CodexProvider(binary=str(fake), env={"AHUB_CODEX_FAKE_DATA": str(DATA)})
    err = (DATA / "sandbox_broken.stderr.txt").read_text(encoding="utf-8")
    monkeypatch.setattr("ahub.providers.codex.run_capture",
                        lambda cmd, **kw: (1, "", err) if cmd[1] == "sandbox" else (0, "", ""))
    ok, why = prov.sandbox_ok()
    assert not ok and why == "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted"


def test_long_prompt_goes_to_file(tmp_path):
    long = "x" * 70_000
    arg = prompt_arg(long, str(tmp_path))
    assert len(arg) < 1000 and "in file" in arg
    files = list((tmp_path / ".ahub").glob("prompt_*.md"))
    assert len(files) == 1 and files[0].read_text() == long


def _real_home() -> dict[str, str]:
    """conftest fakes HOME — codex reads the login from the real ~/.codex."""
    import pwd

    return {"HOME": pwd.getpwuid(os.getuid()).pw_dir}


@pytest.mark.live
def test_live_contract(tmp_path):
    """Live Codex turn (ChatGPT subscription): AHUB_LIVE=1 pytest -m live tests/test_codex_provider.py"""
    from tests import provider_contract as contract

    real_env = _real_home()
    prov = CodexProvider(env=real_env)

    def make(kind: str, cwd: str) -> RunSpec:
        word = "PONG" if kind == "hello" else "PONG2"
        return RunSpec(prompt=f"Reply with exactly: {word}. Do nothing else, do not call tools.",
                       cwd=cwd, model_id="gpt-5.6-luna", log_path=str(tmp_path / f"{kind}.log"),
                       timeout_s=300, idle_s=180, env=real_env)

    contract.check_catalog_and_health(prov)
    contract.check_session_cycle(prov, make, str(tmp_path))


@pytest.mark.live
def test_live_sandbox_probe(tmp_path):
    """The OS sandbox itself, checked live: the main advantage of this provider.

    On a host where it does not start (a container without user namespaces) the probe fails and
    health() says so — codex would fail every command silently.
    """
    prov = CodexProvider(env=_real_home())
    logged_in, said = prov.login()
    assert logged_in, said
    ok, why = prov.sandbox_ok()
    assert ok or why, "the probe must explain itself"
    if not ok:
        pytest.skip(f"the OS sandbox does not start here: {why}")
    r = subprocess.run([shutil.which("codex") or "codex", "sandbox", "workspace-write", "--",
                        "touch", "sandbox_probe.txt"],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
                       env={**os.environ, **_real_home()})
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "sandbox_probe.txt").is_file()  # a write inside the workspace is allowed
