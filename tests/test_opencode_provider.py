"""opencode provider: parsing real events, error classification, command, database path. Live runs are marked live."""

from __future__ import annotations

import json
import os

import pytest

from ahub.providers.base import Act, Cap, Outcome, RunSpec
from ahub.providers.opencode import (OpencodeProvider, classify_error, extract_json, parse_models_verbose,
                                     prompt_arg)

SID = "ses_f0d672de7ffesY3E6FxBC7787R"
REAL = {
    "step_start": {"type": "step_start", "timestamp": 1790776503470, "sessionID": SID,
                   "part": {"id": "prt_1", "messageID": "msg_1", "sessionID": SID, "type": "step-start"}},
    "tool": {"type": "tool_use", "timestamp": 1790776504429, "sessionID": SID,
             "part": {"type": "tool", "tool": "read", "callID": "c1",
                      "state": {"status": "completed", "input": {"filePath": "/x/y"}, "output": "..."}}},
    "finish": {"type": "step_finish", "timestamp": 1790776504482, "sessionID": SID,
               "part": {"reason": "tool-calls", "type": "step-finish",
                        "tokens": {"total": 12483, "input": 9501, "output": 124, "reasoning": 57,
                                   "cache": {"write": 0, "read": 2801}}, "cost": 0.000991902}},
    "text": {"type": "text", "timestamp": 1790776804381, "sessionID": SID,
             "part": {"type": "text", "text": "Готово: отчёт записан."}},
    "err503": {"type": "error", "timestamp": 1790731229889, "sessionID": SID,
               "error": {"name": "APIError", "data": {"message": "The backend is temporarily overloaded. Please retry.",
                                                      "statusCode": 503, "isRetryable": True}}},
    "err_unknown": {"type": "error", "error": "UnknownError: Unexpected server error"},
    "err400": {"type": "error", "sessionID": SID,
               "error": {"name": "APIError", "data": {"message": 'Bad Request: {"model":"deepseek-v4.1-flash"}',
                                                      "statusCode": 400, "isRetryable": False}}},
}


@pytest.fixture
def oc(tmp_path) -> OpencodeProvider:
    return OpencodeProvider(db_path=str(tmp_path / "no.db"), binary="/usr/bin/opencode-fake")


def test_parse_real_events(oc):
    acts = [a for ev in REAL.values() for a in oc.parse_line(json.dumps(ev), 1)]
    kinds = [a.kind for a in acts if a.kind is not Act.SESSION]
    assert kinds[:5] == [Act.STEP, Act.TOOL_END, Act.STEP, Act.USAGE, Act.TEXT]
    assert acts[0].kind is Act.SESSION and acts[0].text == SID and acts[0].ts == 1790776503470
    tool = next(a for a in acts if a.kind is Act.TOOL_END)
    assert tool.tool == "read" and tool.data["status"] == "completed" and tool.data["input"] == {"filePath": "/x/y"}
    usage = next(a for a in acts if a.kind is Act.USAGE).data["usage"]
    assert usage.tokens_in == 9501 and usage.cache_read == 2801 and usage.context == 12302
    assert next(a for a in acts if a.kind is Act.TEXT).text == "Готово: отчёт записан."
    assert oc.parse_line("не json", 1) == [] and oc.parse_line("[1,2]", 1) == []


@pytest.mark.parametrize("key,flag", [("err503", "transient"), ("err_unknown", "transient"), ("err400", None)])
def test_classify_real_errors(key, flag):
    text, flags = classify_error(REAL[key])
    assert text
    for f in ("transient", "quota", "no_access"):
        assert flags[f] is (f == flag), (key, flags)


@pytest.mark.parametrize("ev,flag", [
    ({"type": "error", "error": {"data": {"message": "Unauthorized", "statusCode": 401}}}, "no_access"),
    ({"type": "error", "error": "Usage limit exceeded for your plan (429)"}, "quota"),
    ({"type": "error", "error": "Rate limited, status 429"}, "transient"),
    ({"type": "error", "message": "socket hang up"}, "transient"),
    ({"type": "error", "error": "invalid tool call"}, None),
    ({"type": "error", "error": "took 429ms then failed: invalid json"}, None),
])
def test_classify_forms(ev, flag):
    _text, flags = classify_error(ev)
    for f in ("transient", "quota", "no_access"):
        assert flags[f] is (f == flag), (ev, flags)


def test_classify_outcomes(oc):
    def outcome(evs, rc, sid=SID):
        acts = [a for ev in evs for a in oc.parse_line(json.dumps(ev), 1)]
        return oc.classify(exit_code=rc, activities=acts, session_id=sid, stderr_tail="")[0]

    assert outcome([REAL["text"]], 0) is Outcome.OK
    assert outcome([REAL["err503"]], 1) is Outcome.TRANSIENT
    assert outcome([REAL["err503"], REAL["text"]], 0) is Outcome.OK  # a transient blip with rc=0
    assert outcome([REAL["err400"]], 1) is Outcome.MODEL_ERROR
    assert outcome([], 0, sid=None) is Outcome.CRASH


def test_build_command(oc, tmp_path):
    spec = RunSpec(prompt="привет", cwd=str(tmp_path), model_id="opencode-go/muse-spark-1.3-contributor",
                   variant="xhigh", session_id="ses_1")
    cmd = oc.build_command(spec)
    assert cmd[:4] == ["/usr/bin/opencode-fake", "run", "--format", "json"]
    assert cmd[cmd.index("--model") + 1] == "opencode-go/muse-spark-1.3-contributor"
    assert cmd[cmd.index("--dir") + 1] == str(tmp_path)
    assert cmd[cmd.index("--variant") + 1] == "xhigh" and cmd[cmd.index("--session") + 1] == "ses_1"
    assert cmd[-1] == "привет"
    env = oc.env(spec)
    assert env["AHUB_HOME"].startswith(str(tmp_path)) and os.path.isdir(env["AHUB_HOME"])


def test_long_prompt_goes_to_file(tmp_path):
    long = "x" * 70_000
    arg = prompt_arg(long, str(tmp_path))
    assert len(arg) < 1000 and "in file" in arg
    files = list((tmp_path / ".ahub").glob("prompt_*.md"))
    assert len(files) == 1 and files[0].read_text() == long


def test_extract_json():
    assert extract_json('Итог:\n```json\n{"verdict": "approve", "findings": []}\n```') == {
        "verdict": "approve", "findings": []}
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json("просто текст") is None


def test_parse_models_verbose():
    out = """opencode-go/muse-spark-1.3-contributor
{
  "id": "muse-spark-1.3-contributor",
  "providerID": "opencode-go",
  "status": "active",
  "cost": {"input": 0.1, "output": 0.2},
  "variants": {"low": {}, "xhigh": {"reasoningEffort": "xhigh"}}
}
opencode/muse-spark-1.3-contributor-free
{
  "id": "muse-spark-1.3-contributor-free",
  "providerID": "opencode",
  "cost": {"input": 0, "output": 0}
}
"""
    m = parse_models_verbose(out)
    assert [x.model_id for x in m] == ["opencode-go/muse-spark-1.3-contributor",
                                       "opencode/muse-spark-1.3-contributor-free"]
    assert m[0].counter == "go" and m[0].variants == ("low", "xhigh") and m[0].price_out == 0.2
    assert m[1].counter == "free"


def test_capabilities_and_missing_binary(oc):
    assert oc.has(Cap.RESUME) and oc.has(Cap.STREAM) and not oc.has(Cap.STRUCTURED)
    h = oc.health()
    assert not h.ok and "нет исполняемого opencode" in h.problems[0]


@pytest.mark.live
def test_live_contract(tmp_path):
    """Live Spark session (≈ $0.001): AHUB_LIVE=1 pytest -m live tests/test_opencode_provider.py"""
    import pwd

    from tests import provider_contract as contract

    home = pwd.getpwuid(os.getuid()).pw_dir  # conftest faked HOME — opencode needs the real one (auth)
    real_env = {"HOME": home, "XDG_CONFIG_HOME": f"{home}/.config", "XDG_DATA_HOME": f"{home}/.local/share"}
    real = OpencodeProvider(db_path=f"{home}/.local/share/opencode/opencode.db", env=real_env)

    def make(kind: str, cwd: str) -> RunSpec:
        word = "PONG" if kind == "hello" else "PONG2"
        return RunSpec(prompt=f"Ответь одним словом: {word}. Ничего не делай, инструменты не вызывай.",
                       cwd=cwd, model_id="opencode-go/muse-spark-1.3-contributor", variant="low",
                       log_path=str(tmp_path / f"{kind}.log"), timeout_s=300, idle_s=180, env=real_env)

    contract.check_catalog_and_health(real)
    contract.check_session_cycle(real, make, str(tmp_path))
