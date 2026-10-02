"""Поставщик agy: разбор сохранённых живых образцов, классификация ошибок, команда, каталог,
контракт на фейковом исполняемом файле. Живой — с пометкой live."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ahub.providers.agy import (AgyProvider, classify_stderr, classify_text, parse_models, prompt_arg)
from ahub.providers.base import Act, Cap, Outcome, RunSpec

DATA = Path(__file__).parent / "data" / "agy"
SID = "f3abdf51-18b6-4b8d-b92e-33565bb400a3"


@pytest.fixture
def agy() -> AgyProvider:
    return AgyProvider(binary="/usr/bin/agy-fake")


def acts_of(provider: AgyProvider, sample: str) -> list:
    out = []
    for line in (DATA / sample).read_text(encoding="utf-8").splitlines():
        out.extend(provider.parse_line(line, 1))
    return out


def outcome_of(provider: AgyProvider, sample: str, rc: int, stderr: str = "", sid: str = SID):
    acts = acts_of(provider, sample)
    return provider.classify(exit_code=rc, activities=acts, session_id=sid, stderr_tail=stderr)[0], acts


def test_parse_hello_sample(agy):
    acts = acts_of(agy, "hello.ndjson")
    assert acts[0].kind is Act.SESSION and acts[0].text == SID
    kinds = [a.kind for a in acts]
    assert kinds == [Act.SESSION, Act.STEP, Act.TEXT, Act.TEXT, Act.STEP, Act.USAGE, Act.TEXT, Act.USAGE]
    assert [a.text for a in acts if a.kind is Act.TEXT] == ["OK", "OK\n", "OK\n"]  # дельты и итог
    step = acts[1]
    assert step.data == {"edge": "done", "type": "user_input"}
    assert agy.final_text(acts) == "OK\n"
    u = agy.stream_usage(acts)
    assert (u.tokens_in, u.tokens_out, u.context) == (12508, 1, 12508)  # итог result, не сумма шагов
    assert u.cost_go is None and u.cost_usd is None and u.quota is None  # квота окном — денег нет


def test_parse_tools_sample(agy):
    acts = acts_of(agy, "tools.ndjson")
    tools = [a for a in acts if a.kind in (Act.TOOL_START, Act.TOOL_END)]
    assert [(a.kind, a.tool) for a in tools] == [(Act.TOOL_START, "write_to_file"),
                                                 (Act.TOOL_END, "write_to_file")]
    assert tools[0].data["input"] == {"TargetFile": "/tmp/opencode/agytest/out.txt"}
    texts = [a.text for a in acts if a.kind is Act.TEXT]
    assert texts[0] == "Created" and "out.txt" in texts[-1]  # дельты склеены в буфер шага
    assert agy.final_text(acts).startswith("Created")


def test_parse_resume_sample(agy):
    acts = acts_of(agy, "resume.ndjson")
    assert acts[0].kind is Act.SESSION and acts[0].text == SID  # тот же разговор
    assert any(a.kind is Act.STEP and a.data.get("type") == "system_message" for a in acts)
    assert agy.final_text(acts) == "PONG2\n"
    assert agy.stream_usage(acts).tokens_out == 7


def test_parallel_turns_keep_own_deltas(agy):
    """Ревьюеры идут параллельно в одном процессе: буферы текста не должны мешать друг другу."""
    def line(cid: str, index: int, delta: str, state: str = "ACTIVE") -> str:
        return json.dumps({"event": "step_update", "step_update": {"conversation_id": cid, "step_index": index,
                                                                 "state": state, "step_type": "agent_response",
                                                                 "text_delta": delta}})

    agy.parse_line(json.dumps({"event": "init", "conversation_id": "A"}), 1)
    agy.parse_line(json.dumps({"event": "init", "conversation_id": "B"}), 1)
    assert [a.text for a in agy.parse_line(line("A", 1, "AAA"), 1)] == ["AAA"]
    assert [a.text for a in agy.parse_line(line("B", 1, "BBB"), 1)] == ["BBB"]
    assert [a.text for a in agy.parse_line(line("A", 1, " more"), 1)] == ["AAA more"]


def test_structured_from_result(agy):
    acts = acts_of(agy, "structured.ndjson")
    got = agy.structured(agy.final_text(acts), acts, {"type": "object"})
    assert got == {"score": 100, "verdict": "Yes, 2 + 2 is equal to 4."}
    assert agy.structured("просто текст", [], None) is None


def test_outcomes_of_real_samples(agy):
    assert outcome_of(agy, "hello.ndjson", 0)[0] is Outcome.OK
    assert outcome_of(agy, "tools.ndjson", 0)[0] is Outcome.OK
    assert outcome_of(agy, "structured.ndjson", 0)[0] is Outcome.OK
    assert outcome_of(agy, "model_error.ndjson", 1)[0] is Outcome.MODEL_ERROR
    assert outcome_of(agy, "network_error.ndjson", 1)[0] is Outcome.TRANSIENT
    # с --print-timeout agy вышел с кодом 0 при status ERROR — решает событие, не код
    rc, acts = outcome_of(agy, "network_error.ndjson", 0,
                          (DATA / "print_timeout.stderr.txt").read_text(encoding="utf-8"))
    assert rc is Outcome.TRANSIENT
    assert "connection refused" in agy.classify(exit_code=0, activities=acts, session_id=SID,
                                               stderr_tail="")[1]


def test_denied_action_is_not_success(agy):
    stderr = (DATA / "denied_command.stderr.txt").read_text(encoding="utf-8")
    out, text = agy.classify(exit_code=0, activities=acts_of(agy, "denied_command.ndjson"),
                             session_id=SID, stderr_tail=stderr)
    assert out is Outcome.MODEL_ERROR
    assert "RunCommand" in text and "auto-denied" in text  # из stderr — что делать


def test_print_timeout_outcome(agy):
    acts = acts_of(agy, "hello.ndjson")
    out, text = agy.classify(exit_code=0, activities=acts, session_id=SID,
                             stderr_tail="[agy] print timeout after 60s with turn in progress\n")
    assert out is Outcome.TIMEOUT and "print timeout" in text


def test_unknown_conversation_still_ok(agy):
    out, _ = agy.classify(exit_code=0, activities=acts_of(agy, "hello.ndjson"), session_id="new",
                          stderr_tail='warning: conversation "old-id" not found\n')
    assert out is Outcome.OK  # agy открыл новую сессию вместо продолжения — ход состоялся


@pytest.mark.parametrize("text,flag", [
    ('API error (attempt 4): request failed: Post "http://127.0.0.1:9/v1beta/models/gemini-3.8-flash:'
     'streamGenerateContent?alt=sse": dial tcp 127.0.0.1:9: connect: connection refused', "transient"),
    ("deadline exceeded while calling the model", "transient"),
    ("got status 503 from the model endpoint", "transient"),
    ("You have exceeded your current quota", "quota"),
    ("RESOURCE_EXHAUSTED: rate limit reached", "quota"),
    ("status 429 Too Many Requests", "quota"),
    ("401 Unauthorized: credentials invalid", "no_access"),
    ("permission denied for this model", "no_access"),
    ('invalid model selection (--model "gemini-nope"): model gemini-nope is not recognized', None),
    ("the response was cut short", None),
])
def test_classify_forms(text, flag):
    flags = classify_text(text)
    for f in ("transient", "quota", "no_access"):
        assert flags[f] is (f == flag), (text, flags)


def test_classify_agy_error_line():
    line = ('AGY_ERROR: {"short_error": "upstream request failed", "http_status": 503, '
            '"retryable": true, "error_id": "abc-123"}')
    text, flags = classify_stderr("noise\n" + line)
    assert "upstream request failed" in text and flags["transient"] is True and flags["status"] == 503
    text, flags = classify_stderr('AGY_ERROR: {"short_error": "quota exhausted"}')
    assert flags["quota"] is True
    assert classify_stderr("no structured error here") == ("", {"transient": False, "quota": False,
                                                               "no_access": False, "status": None})


def test_build_command(agy, tmp_path):
    spec = RunSpec(prompt="привет", cwd=str(tmp_path), model_id="gemini-3.8-flash-high", session_id=SID,
                   timeout_s=1234)
    cmd = agy.build_command(spec)
    assert cmd[:6] == ["/usr/bin/agy-fake", "-p", "привет", "--output-format", "stream-json", "--model"]
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-high"
    assert cmd[cmd.index("--print-timeout") + 1] == "1234s"
    assert cmd[cmd.index("--conversation") + 1] == SID
    assert cmd[-1] == "--dangerously-skip-permissions"
    assert "--json-schema" not in cmd

    spec.schema = {"type": "object", "properties": {"verdict": {"type": "string"}}}
    assert json.loads(agy.build_command(spec)[agy.build_command(spec).index("--json-schema") + 1]) == spec.schema

    edged = AgyProvider(binary="/usr/bin/agy-fake", skip_permissions=False).build_command(spec)
    assert edged[-2:] == ["--mode", "accept-edits"] and "--dangerously-skip-permissions" not in edged


def test_long_prompt_goes_to_file(tmp_path):
    long = "x" * 70_000
    arg = prompt_arg(long, str(tmp_path))
    assert len(arg) < 1000 and "in file" in arg
    files = list((tmp_path / ".ahub").glob("prompt_*.md"))
    assert len(files) == 1 and files[0].read_text() == long


def test_capabilities_and_missing_binary(agy):
    assert agy.has(Cap.RESUME) and agy.has(Cap.STREAM) and agy.has(Cap.STRUCTURED) and agy.has(Cap.TOKENS)
    assert not agy.has(Cap.COST_MONEY) and not agy.has(Cap.COST_QUOTA)  # квота окном, цен нет
    assert not agy.has(Cap.EXPORT) and not agy.has(Cap.FIND_SESSION)
    h = agy.health()
    assert not h.ok and "нет исполняемого agy" in h.problems[0]


def test_parse_models():
    models = parse_models((DATA / "models.txt").read_text(encoding="utf-8"))
    assert [m.model_id for m in models][:3] == ["gemini-3.8-flash-high", "gemini-3.8-flash-medium",
                                                 "gemini-3.8-flash-low"]
    assert models[0].note == "Gemini 3.8 Flash (High)" and models[0].counter == "quota"
    assert len(models) == 14 and parse_models("") == []


def _fake_agy(tmp_path: Path) -> Path:
    """Исполняемый файл agy: тело tests/data/agy/fake_agy.py под shebangом текущего питона."""
    from tests.provider_contract import fake_agy

    return fake_agy(tmp_path)


def test_contract_on_fake_binary(tmp_path):
    """Общий набор контракта на фейковом исполняемом файле: сети нет, ответы — живые образцы."""
    from tests import provider_contract as contract

    fake = _fake_agy(tmp_path)
    env = {"AHUB_AGY_FAKE_DATA": str(DATA)}
    contract.agy_state(tmp_path)  # вход есть — health() должен быть зелёным
    prov = AgyProvider(binary=str(fake), env=env)

    def make(kind: str, cwd: str) -> RunSpec:
        word = "PONG" if kind == "hello" else "PONG2"
        return RunSpec(prompt=f"Ответь одним словом: {word}", cwd=cwd, model_id="gemini-3.8-flash-low",
                       log_path=str(tmp_path / f"{kind}.log"), timeout_s=60, idle_s=30, env=env)

    contract.check_catalog_and_health(prov)
    contract.check_session_cycle(prov, make, str(tmp_path))


def test_health_reports_login_and_models(tmp_path):
    fake = _fake_agy(tmp_path)
    prov = AgyProvider(binary=str(fake), env={"AHUB_AGY_FAKE_DATA": str(DATA)})
    h = prov.health()  # ни входа, ни файла состояния — обе проблемы видны
    assert not h.ok and len(h.problems) == 1 and "о входе" in h.problems[0]
    assert h.details["models"] == 14 and h.details["version"] == "1.2.15-fake"


@pytest.mark.live
def test_live_contract(tmp_path):
    """Живой ход Gemini (квота окном): AHUB_LIVE=1 pytest -m live tests/test_agy_provider.py"""
    import pwd

    from tests import provider_contract as contract

    home = pwd.getpwuid(os.getuid()).pw_dir  # conftest подменил HOME — agy нужен настоящий (вход, настройки)
    real_env = {"HOME": home}
    prov = AgyProvider(env=real_env)

    def make(kind: str, cwd: str) -> RunSpec:
        word = "PONG" if kind == "hello" else "PONG2"
        return RunSpec(prompt=f"Ответь одним словом: {word}. Ничего не делай, инструменты не вызывай.",
                       cwd=cwd, model_id="gemini-3.8-flash-low", log_path=str(tmp_path / f"{kind}.log"),
                       timeout_s=300, idle_s=180, env=real_env)

    contract.check_catalog_and_health(prov)
    contract.check_session_cycle(prov, make, str(tmp_path))


