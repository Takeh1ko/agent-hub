"""Engine on code and routine tasks: gates, repair, acceptance, review panel, rounds, budget, prepare."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import tasks
from ahub.engine import Engine
from ahub.model import Kind, Phase, State
from ahub.providers.agy import AgyProvider
from ahub.providers.base import Act, Activity
from ahub.providers.codex import CodexProvider
from ahub.store import Store
from tests.enginekit import git, install_fake, make_project


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def project(tmp_path):
    return make_project(tmp_path)


def code_task(store, project, **kw):
    kw.setdefault("paths", ["core/**", "tests/**"])
    kw.setdefault("accept", ["tests/test_a.py::test_x"])
    kw.setdefault("review_level", 0)
    kind = kw.pop("kind", Kind.CODE)
    return tasks.create(store, tasks.TaskSpec(project="P", kind=kind, title="починить", model="fake", **kw),
                        project, collect=False)


def run(store, project, tid):
    return Engine(store, project, tid, sleep=lambda s: None).run()


def work(session="ses_x", text="Y = 2\n", path="core/b.py", cost=0.01, files=None):
    return {"session": session, "steps": [
        {"event": {"type": "usage", "in": 100, "out": 10, "go": cost}},
        {"write": {"path": path, "text": text}},
        {"git_commit": "feat: сделал"},
        {"result": {"summary": "сделал", "files": files if files is not None else [path]}},
        {"event": {"type": "text", "text": "готово"}}]}


def verdict(round_no=1, v="approve", findings=None, model="fake", session="ses_rev"):
    body = {"verdict": v, "summary": "ок", "findings": findings or []}
    return {"session": session, "steps": [
        {"write": {"path": f".ahub/review_r{round_no}_{model}.json", "text": json.dumps(body, ensure_ascii=False)}},
        {"event": {"type": "text", "text": "готово"}}]}


HIGH = [{"severity": "high", "file": "core/b.py", "line": 1, "issue": "Y должен быть 3", "fix": "поставить 3"}]


def test_code_no_review(store, project):
    fake = install_fake(store, [work()])
    t = code_task(store, project)
    res = run(store, project, t.id)
    assert res.state is State.DONE, res.reason
    done = store.events(task_id=t.id, needs_reaction=True)[-1].payload
    assert done["tests"] == "green" and "1 file changed" in done["diffstat"] and done["summary"] == "сделал"
    assert "Allowed files" in fake.calls[0]["prompt"] and "tests/test_a.py::test_x" in fake.calls[0]["prompt"]


def test_review_approve(store, project):
    fake = install_fake(store, [work(), verdict()])
    t = code_task(store, project, review_models=["fake"], review_rounds=2)
    assert run(store, project, t.id).state is State.DONE
    rev_prompt = fake.calls[1]["prompt"]
    assert "diff --git" in rev_prompt and "core/b.py" in rev_prompt and fake.calls[1]["session_id"] is None


def test_review_changes_then_fix(store, project):
    fake = install_fake(store, [work(), verdict(v="changes", findings=HIGH),
                                work(text="Y = 3\n"), verdict(round_no=2)])
    t = code_task(store, project, review_models=["fake"], review_rounds=2)
    assert run(store, project, t.id).state is State.DONE
    assert store.get_task(t.id).round == 2
    assert "Y должен быть 3" in fake.calls[2]["prompt"] and fake.calls[2]["session_id"] == "ses_x"
    kinds = [e.payload.get("to") for e in store.events(task_id=t.id) if e.kind == "state"]
    assert kinds[:6] == ["preparing", "working", "checking", "reviewing", "fixing", "checking"]


def test_rounds_exhausted(store, project):
    install_fake(store, [work(), verdict(v="changes", findings=HIGH)])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "круги ревью кончились" in res.reason and "high: 1" in res.reason


def test_low_findings_do_not_block(store, project):
    low = [{"severity": "low", "file": "core/b.py", "line": 1, "issue": "имя переменной"}]
    install_fake(store, [work(), verdict(v="changes", findings=low)])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    assert run(store, project, t.id).state is State.DONE


def test_outside_allowed_is_decision(store, project):
    install_fake(store, [work(path="docs/x.md")])
    t = code_task(store, project, paths=["core/**", "tests/**"])
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "вне разрешённых: docs/x.md" in res.reason


def test_no_commit_repair(store, project):
    nocommit = {"session": "ses_x", "steps": [{"write": {"path": "core/b.py", "text": "Y = 2\n"}},
                                              {"event": {"type": "text", "text": "готово"}}]}
    fix = {"session": "ses_x", "steps": [{"git_commit": "feat: b"}, {"result": {"summary": "ок", "files": ["core/b.py"]}}]}
    fake = install_fake(store, [nocommit, fix])
    t = code_task(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert "Result is not in the required form" in fake.calls[1]["prompt"] and "незакоммиченные" in fake.calls[1]["prompt"]


def test_red_tests_fixed_once(store, project):
    red = work(path="core/a.py", text="X = 0\n")
    green = work(path="core/a.py", text="X = 1\n# fixed\n")
    fake = install_fake(store, [red, green])
    t = code_task(store, project)
    assert run(store, project, t.id).state is State.DONE
    assert "Failing acceptance" in fake.calls[1]["prompt"]


def test_red_tests_twice(store, project):
    red = work(path="core/a.py", text="X = 0\n")
    install_fake(store, [red, work(path="core/a.py", text="X = 5\n")])
    t = code_task(store, project)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "приёмка красная" in res.reason


def test_routine_no_tests(store, project):
    install_fake(store, [work(path="docs/readme.md", text="# порядок\n")])
    t = code_task(store, project, kind=Kind.ROUTINE, paths=["docs/**"], accept=[])
    res = run(store, project, t.id)
    assert res.state is State.DONE and "приёмка" not in res.reason


def test_budget_exhausted_stops_with_save(store, project):
    stop_step = {"session": "ses_x", "steps": [{"event": {"type": "text", "text": "сохранил"}}]}
    fake = install_fake(store, [work(cost=0.02), stop_step])
    t = code_task(store, project, review_models=["fake"], budget_go=0.01)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "бюджет исчерпан" in res.reason
    assert "asks you to stop" in fake.calls[1]["prompt"] and fake.calls[1]["session_id"] == "ses_x"
    assert "budget_hard" in [e.kind for e in store.events(task_id=t.id)]


def test_hook_failure_is_error(store, tmp_path):
    project = make_project(tmp_path, hooks={"task_setup": "echo нет базы; exit 3"})
    install_fake(store, [])
    t = code_task(store, project)
    res = run(store, project, t.id)
    assert res.state is State.ERROR and "хук task_setup: код 3" in res.reason


def test_secrets_hidden_from_copy(store, tmp_path):
    project = make_project(tmp_path, secrets={"exclude": ["*.secret"]})
    root = Path(project.root)
    (root / "core" / "keys.secret").write_text("TOKEN=123\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "секрет")
    install_fake(store, [work()])
    t = code_task(store, project)
    assert run(store, project, t.id).state is State.DONE
    wt = Path(store.get_task(t.id).worktree)
    assert not (wt / "core" / "keys.secret").exists() and (wt / "core" / "a.py").exists()


def test_reviewer_changes_reverted(store, project):
    naughty = verdict()
    naughty["steps"].insert(0, {"write": {"path": "core/b.py", "text": "испорчено\n"}})
    install_fake(store, [work(), naughty])
    t = code_task(store, project, review_models=["fake"])
    assert run(store, project, t.id).state is State.DONE
    assert (Path(store.get_task(t.id).worktree) / "core" / "b.py").read_text() == "Y = 2\n"


def silent_review(session="ses_rev", text="разбор текстом без файла"):
    return {"session": session, "steps": [{"event": {"type": "text", "text": text}}]}


def test_reviewer_retry_same_session(store, project):
    fake = install_fake(store, [work(), silent_review(), verdict(session="ses_other")])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    res = run(store, project, t.id)
    assert res.state is State.DONE, res.reason
    assert len(fake.calls) == 3
    assert fake.calls[1]["session_id"] is None
    assert fake.calls[2]["session_id"] == "ses_rev"
    prompt = fake.calls[2]["prompt"]
    assert ".ahub/review_r1_fake.json" in prompt and "You did not write" in prompt
    assert "Do not change any other files" in prompt


def test_reviewer_missing_twice_is_decision(store, project):
    fake = install_fake(store, [work(), silent_review(), silent_review(session="ses_rev2")])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "не сдали вердикт" in res.reason
    assert len(fake.calls) == 3
    assert fake.calls[2]["session_id"] == "ses_rev"


def test_reviewer_no_retry_without_session(store, project):
    hidden = {"session": "", "steps": [{"event": {"type": "text", "text": "тихо"}}]}
    fake = install_fake(store, [work(), hidden])
    t = code_task(store, project, review_models=["fake"], review_rounds=1)
    res = run(store, project, t.id)
    assert res.state is State.NEEDS_DECISION and "не сдали вердикт" in res.reason
    assert len(fake.calls) == 2


# --- phases by activity (opencode and agy tools) ---

AGY_DATA = Path(__file__).parent / "data" / "agy"
CODEX_DATA = Path(__file__).parent / "data" / "codex"


class Phases(Engine):
    """Engine that remembers phases: only the last one is visible in a task."""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self.seen: list[str] = []

    def set_phase(self, phase: Phase) -> None:
        self.seen.append(phase.value)
        super().set_phase(phase)


def phases_of(eng: Phases) -> list[str]:
    """Phases in a row: the same phase again (tools come in bursts) collapses."""
    out: list[str] = []
    for phase in eng.seen:
        if not out or out[-1] != phase:
            out.append(phase)
    return out


def feed(eng: Phases, acts) -> None:
    for act in acts:
        eng._on_activity(act)


def agy_tool(tool: str, params: dict | None = None):
    """agy activity for a tool: the parameters arrive in tool_info.parameters."""
    line = json.dumps({"event": "step_update", "step_update": {
        "conversation_id": "c1", "step_index": 1, "state": "ACTIVE", "step_type": "tool",
        "tool_name": tool, "tool_info": {"name": tool, "parameters": params or {}}}})
    return AgyProvider(binary="agy").parse_line(line, 1)


def codex_item(itype: str, **fields):
    """codex activity for a tool: `codex exec --json` reports tools as items, the command as `command`."""
    item = {"id": "item_0", "type": itype, **fields}
    return CodexProvider(binary="codex").parse_line(json.dumps({"type": "item.started", "item": item}), 1)


def test_opencode_tool_phases(store, project):
    """opencode tools: write — writing, read — studying, bash with pytest — testing."""
    install_fake(store, [])
    t = code_task(store, project)
    eng = Phases(store, project, t.id, sleep=lambda s: None)
    cases = (("edit", {}, Phase.WRITING), ("read", {}, Phase.STUDYING),
             ("bash", {"input": {"command": "pytest -q"}}, Phase.TESTING),
             ("bash", {"input": {"command": "git log --oneline"}}, Phase.STUDYING))
    for tool, data, want in cases:
        eng.seen.clear()
        feed(eng, [Activity(Act.TOOL_START, 1, tool=tool, data=data)])
        assert phases_of(eng) == [want.value], tool


def test_agy_write_and_read_tools(store, project):
    """agy tools: write — writing, read — studying (live sample plus tool names)."""
    install_fake(store, [])
    t = code_task(store, project)
    eng = Phases(store, project, t.id, sleep=lambda s: None)
    for line in (AGY_DATA / "tools.ndjson").read_text(encoding="utf-8").splitlines():
        feed(eng, AgyProvider(binary="agy").parse_line(line, 1))
    assert phases_of(eng) == [Phase.WRITING.value]  # write_to_file from the live sample
    for tool in ("view_file", "list_dir", "grep_search", "find_by_name"):
        eng.seen.clear()
        feed(eng, agy_tool(tool, {"path": "core/a.py"}))
        assert phases_of(eng) == [Phase.STUDYING.value], tool
    for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file"):
        eng.seen.clear()
        feed(eng, agy_tool(tool, {"path": "core/a.py"}))
        assert phases_of(eng) == [Phase.WRITING.value], tool


def test_agy_command_tool(store, project):
    """run_command: pytest in the command — testing, any other command — studying."""
    install_fake(store, [])
    t = code_task(store, project)
    eng = Phases(store, project, t.id, sleep=lambda s: None)
    feed(eng, agy_tool("run_command", {"CommandLine": ".venv/bin/python -m pytest -q tests"}))
    assert phases_of(eng) == [Phase.TESTING.value]
    eng.seen.clear()
    feed(eng, agy_tool("run_command", {"CommandLine": "git status"}))
    assert phases_of(eng) == [Phase.STUDYING.value]


def test_agy_tools_phases_end_to_end(store, project):
    """agy in a fake stream: view_file → studying, write_to_file → writing, gate → testing."""
    scenario = {"session": "ses_x", "steps": [
        {"event": {"type": "tool_start", "tool": "view_file"}},
        {"event": {"type": "tool_start", "tool": "grep_search"}},
        {"event": {"type": "tool_start", "tool": "write_to_file"}},
        {"event": {"type": "usage", "in": 100, "out": 10, "go": 0.01}},
        {"write": {"path": "core/b.py", "text": "Y = 2\n"}},
        {"git_commit": "feat: сделал"},
        {"result": {"summary": "сделал", "files": ["core/b.py"]}},
        {"event": {"type": "text", "text": "готово"}}]}
    install_fake(store, [scenario])
    t = code_task(store, project)
    eng = Phases(store, project, t.id, sleep=lambda s: None)
    assert eng.run().state is State.DONE
    assert phases_of(eng) == [Phase.WRITING.value, Phase.STUDYING.value, Phase.WRITING.value,
                              Phase.TESTING.value]


def test_codex_write_and_command_tools(store, project):
    """codex tools: file_change — writing, command_execution with pytest — testing, else studying."""
    install_fake(store, [])
    t = code_task(store, project)
    eng = Phases(store, project, t.id, sleep=lambda s: None)
    for line in (CODEX_DATA / "commands.ndjson").read_text(encoding="utf-8").splitlines():
        feed(eng, CodexProvider(binary="codex").parse_line(line, 1))
    assert phases_of(eng) == [Phase.STUDYING.value]  # `echo hello > sbox.txt` from the live sample

    eng.seen.clear()
    feed(eng, codex_item("file_change", changes=[{"path": "core/a.py", "kind": "modify"}]))
    assert phases_of(eng) == [Phase.WRITING.value]
    eng.seen.clear()
    feed(eng, codex_item("command_execution", command="/bin/bash -lc '.venv/bin/python -m pytest -q tests'"))
    assert phases_of(eng) == [Phase.TESTING.value]  # pytest inside the command line
    eng.seen.clear()
    feed(eng, codex_item("command_execution", command="/bin/bash -lc 'git status'"))
    assert phases_of(eng) == [Phase.STUDYING.value]
