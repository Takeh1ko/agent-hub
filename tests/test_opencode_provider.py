"""opencode provider: parsing real events, error classification, command, database path. Live runs are marked live."""

from __future__ import annotations

import json
import os

import pytest

from ahub.providers.base import Act, Cap, Outcome, RunSpec
from ahub.providers.opencode import (
    OpencodeProvider,
    _short_output,
    classify_error,
    extract_json,
    parse_models_verbose,
    prompt_arg,
)

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


def test_short_output_cuts_and_unwraps():
    """A tool output of any shape becomes 300 characters of text (the transcript shows them)."""
    assert _short_output("hello") == "hello"
    assert _short_output("x" * 500) == "x" * 300  # the cut
    assert _short_output({"output": "from output"}) == "from output"
    assert _short_output({"text": "from text"}) == "from text"
    assert _short_output({"other": "nothing known"}) == ""
    assert _short_output(["a", {"output": "b"}, 3, "c"]) == "a b"  # the list, only the first three
    assert _short_output("y" * 400) in _short_output([{"output": "y" * 400}])  # and the cut recurses
    assert _short_output(None) == "" and _short_output(7) == ""


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


def test_route_from_runspec_never_mutates_singleton(tmp_path):
    """Route comes from immutable RunSpec context; an explicit instance never mutates the cache."""
    from ahub import providers as _providers

    before = dict(os.environ)
    a = OpencodeProvider(db_path=str(tmp_path / "a.db"))
    b = OpencodeProvider(db_path=str(tmp_path / "b.db"))
    assert a.db_path != b.db_path  # per-instance, not a mutated singleton
    spec = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m", session_id="ses_1")
    frozen = (spec.prompt, spec.cwd, spec.model_id, spec.session_id)
    a.build_command(spec)
    a.env(spec)
    assert (spec.prompt, spec.cwd, spec.model_id, spec.session_id) == frozen
    assert dict(os.environ) == before
    cached = _providers.get("opencode")
    assert getattr(cached, "db_path", None) is None  # singleton stays default-routed


def test_fresh_process_reads_same_legacy_session(tmp_path):
    """A fresh provider/CLI process reads usage/state/find/export for the same legacy session."""
    import sqlite3

    from tests.test_opencode_db import NOW as _NOW
    from tests.test_opencode_db import _mkdb, _ses

    db = tmp_path / "opencode.db"
    con = _mkdb(db)
    _ses(con, "ses_fresh", directory=str(tmp_path), cost=0.1, tin=11, t_updated=_NOW)
    con.commit()
    con.close()
    first = OpencodeProvider(db_path=str(db), binary="/nonexistent/opencode-fake")
    assert first.usage("ses_fresh").tokens_in == 11
    assert first.session_state("ses_fresh").usage.tokens_in == 11
    assert first.find_session(str(tmp_path), _NOW - 60_000) == "ses_fresh"
    fresh = OpencodeProvider(db_path=str(db), binary="/nonexistent/opencode-fake")
    assert fresh.usage("ses_fresh").tokens_in == 11  # fresh process, same legacy DB
    assert fresh.export("ses_fresh") is None  # bad binary: honest None, no fabricated export
    assert sqlite3.connect(str(db)).execute("SELECT count(*) FROM session").fetchone()[0] == 1


def test_explicit_db_precedence_over_default(tmp_path, monkeypatch):
    """Explicit db_path beats OPENCODE_DB routing and the default DB (contract 3)."""
    from ahub import paths as _paths
    from tests.conftest import write as _write
    from tests.test_opencode_db import NOW as _NOW
    from tests.test_opencode_db import _mkdb, _ses

    default_db = tmp_path / "default.db"
    explicit_db = tmp_path / "explicit.db"
    for db, sid in ((default_db, "s_def"), (explicit_db, "s_exp")):
        con = _mkdb(db)
        _ses(con, sid, directory="/wt/T", t_updated=_NOW)
        con.commit()
        con.close()
    _write(_paths.global_config_path(), f'[paths]\nopencode_db = "{default_db}"\n')
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    prov = OpencodeProvider(db_path=str(explicit_db))
    assert prov.usage("s_exp") is not None and prov.usage("s_def") is None
    assert prov.find_session("/wt/T", _NOW - 60_000) == "s_exp"


def _routed_spec(cwd: str, route: str) -> RunSpec:
    return RunSpec(prompt="x", cwd=cwd, model_id="m",
                   env={"AHUB_OPENCODE_ROUTE": route, "AHUB_OPENCODE_ISOLATE": "1"})


def test_env_injects_routed_shard_strict(tmp_path, monkeypatch):
    """Child env gets OPENCODE_DB for enrolled routes; explicit/config/spec win; hub env clean."""
    from ahub import paths as _paths
    from ahub.providers import opencode_storage as _storage
    from tests.conftest import write as _write

    monkeypatch.delenv("OPENCODE_DB", raising=False)
    prov = OpencodeProvider()
    assert "OPENCODE_DB" not in prov.env(RunSpec(prompt="x", cwd=str(tmp_path), model_id="m"))
    route = "task:1:role:executor:log:exec"
    shard = _storage.register(_routed_spec(str(tmp_path), route), 1_700_000_000_000)
    assert prov.env(_routed_spec(str(tmp_path), route))["OPENCODE_DB"] == str(shard)
    keep = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m",
                   env={"OPENCODE_DB": "/tmp/keep.db", "AHUB_OPENCODE_ROUTE": route,
                        "AHUB_OPENCODE_ISOLATE": "1"})
    assert prov.env(keep)["OPENCODE_DB"] == "/tmp/keep.db"
    assert "OPENCODE_DB" not in os.environ
    single = OpencodeProvider(db_path="/tmp/explicit.db").env(_routed_spec(str(tmp_path), route))
    assert single["OPENCODE_DB"] == "/tmp/explicit.db"
    _write(_paths.global_config_path(), f'[paths]\nopencode_db = "{tmp_path / "cfg.db"}"\n')
    assert prov.env(_routed_spec(str(tmp_path), "task:9:role:e:log:z"))["OPENCODE_DB"] == str(tmp_path / "cfg.db")


def test_db_for_session_first_recovery_strict(tmp_path):
    """_db_for prefers explicit, then isolate registration, then session mapping, else None."""
    from ahub.providers import opencode_storage as _storage

    assert OpencodeProvider(db_path="/tmp/x.db")._db_for(_routed_spec(str(tmp_path), "task:1:role:e:log:x")) == \
        "/tmp/x.db"
    route = "task:2:role:executor:log:exec"
    spec = _routed_spec(str(tmp_path), route)
    shard = _storage.register(spec, 1_700_000_000_000)
    assert OpencodeProvider()._db_for(spec) == str(shard)
    _storage.bind_session("ses_mapped", shard)
    resume = RunSpec(prompt="x", cwd=str(tmp_path), model_id="m", session_id="ses_mapped")
    assert OpencodeProvider()._db_for(resume) == str(shard)
    assert OpencodeProvider()._db_for(RunSpec(prompt="x", cwd=str(tmp_path), model_id="m")) is None


def test_run_lock_narrow_and_explicit_bypass_strict(tmp_path):
    """Default provider takes the shard namespace lock; explicit db_path stays lock-free."""
    from contextlib import nullcontext

    from ahub.providers import opencode_storage as _storage

    assert isinstance(OpencodeProvider(db_path="/tmp/x.db").run_lock(
        _routed_spec(str(tmp_path), "task:1:role:e:log:x")), type(nullcontext()))
    lock = OpencodeProvider().run_lock(_routed_spec(str(tmp_path), "task:1:role:e:log:x"))
    assert not isinstance(lock, type(nullcontext()))
    with lock:
        assert _storage.register(_routed_spec(str(tmp_path), "task:1:role:e:log:x"), 1) is not None


def test_bind_and_find_session_for_run_strict(tmp_path):
    """bind_session persists the mapping; find_session_for_run reads the routed shard."""
    from ahub.providers import opencode_storage as _storage
    from tests.test_opencode_db import NOW as _NOW
    from tests.test_opencode_db import _mkdb, _ses

    route = "task:4:role:executor:log:exec"
    spec = _routed_spec(str(tmp_path / "wt"), route)
    shard = _storage.register(spec, _NOW - 60_000)
    con = _mkdb(shard)
    _ses(con, "shard_ses", directory=str(tmp_path / "wt"), t_created=_NOW - 10_000, t_updated=_NOW - 10_000)
    con.commit()
    con.close()
    prov = OpencodeProvider(binary="/nonexistent/opencode-fake")
    prov.bind_session("shard_ses", spec)
    assert _storage.session_db("shard_ses") == shard
    assert prov.find_session_for_run(str(tmp_path / "wt"), _NOW - 60_000, spec) == "shard_ses"


def test_export_injects_shard_db_strict(tmp_path, monkeypatch):
    """export() passes OPENCODE_DB of the mapped shard; bad binary stays honest None."""
    from ahub.providers import opencode_storage as _storage

    route = "task:6:role:executor:log:exec"
    spec = _routed_spec(str(tmp_path), route)
    shard = _storage.register(spec, 1_700_000_000_000)
    _storage.bind_session("ses_exp", shard)
    seen: dict = {}
    import ahub.providers.opencode as _opmod

    def _fake(cmd, timeout=120, env=None):
        seen.update(env or {})
        return 1, "", "nope"

    monkeypatch.setattr(_opmod, "run_capture", _fake)
    assert OpencodeProvider(binary="/nonexistent/opencode-fake").export("ses_exp") is None
    assert seen.get("OPENCODE_DB") == str(shard)


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
