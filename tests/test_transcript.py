"""Transcript of a worker session: built from saved real samples, from what the engine writes,
and followed live (`ahub follow`)."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

from ahub import cli, providers, tasks, transcript, transitions
from ahub.engine import Engine
from ahub.model import Kind, State
from ahub.providers.agy import AgyProvider
from ahub.providers.codex import CodexProvider
from ahub.providers.opencode import OpencodeProvider
from ahub.store import Store
from tests.enginekit import install_fake, make_project, scout_ok

DATA = Path(__file__).parent / "data"


# Providers are factories, not instances: agy keeps the text buffers of the streams it parsed, and
# `ahub follow` itself has one fresh instance per process.
@pytest.fixture
def oc():
    return lambda: OpencodeProvider(db_path="/nonexistent/opencode.db", binary="/bin/true")


@pytest.fixture
def agy():
    return lambda: AgyProvider(binary="/bin/true")


@pytest.fixture
def codex():
    return lambda: CodexProvider(binary="/bin/true", sandbox="")


def copy_sample(sample: str, tmp_path: Path, prompts: list[dict] | None = None) -> Path:
    """A saved provider log as a session log, with a prompts sidecar."""
    log = tmp_path / "executor.log"
    log.write_text((DATA / sample).read_text(encoding="utf-8"), encoding="utf-8")
    with transcript.prompts_path(log).open("w", encoding="utf-8") as f:
        for p in prompts or []:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    return log


def render(provider, log: Path, *, full: bool = False, color: bool = False) -> str:
    out = io.StringIO()
    writer = transcript.Writer(out, full=full, color=color, width=100)
    writer.write(transcript.Reader(provider, log).read())
    writer.flush()
    return out.getvalue()


def items_of(provider, log: Path) -> list[transcript.Item]:
    return transcript.Reader(provider, log).read().items


# --- real samples ---


def test_opencode_sample(tmp_path, oc):
    log = copy_sample("opencode/session.ndjson", tmp_path, [
        {"ts": 1790776503000, "turn": 1, "kind": "start", "text": "Почини округление суммы."},
        {"ts": 1790776803000, "turn": 2, "kind": "repair", "text": "Приёмка красная: 1 failed."},
    ])
    items = items_of(oc(), log)
    assert [(i.kind, i.turn) for i in items][:4] == [("reasoning", 1), ("text", 1), ("tool", 1), ("result", 1)]
    assert [(i.kind, i.turn) for i in items if i.kind == "error"] == [("error", 2)]
    # every turn of the stream got its own header (opencode stamps its events, so the time decides)
    assert {i.turn for i in items} == {1, 2}

    text = render(oc(), log)
    assert "── Turn 1 · start · " in text and "── Turn 2 · repair · " in text
    assert "  Почини округление суммы." in text  # the prompt of the turn, indented
    assert "размышление: Сначала посмотрю" in text
    assert "Смотрю расчёт суммы." in text
    assert "🔎 grep  def total" in text and "▶ bash  .venv/bin/python -m pytest -q tests" in text
    assert "✎ edit  /w/core/a.py" in text and "👁 read  /w/core/a.py" in text
    assert "    core/a.py:12:def total(items):" in text  # the result of a tool, indented
    assert "  ✔ completed" in text and "  ✖ error" in text  # ok and failed
    assert "✖ APIError | The backend is temporarily overloaded." in text  # an error of the turn
    assert "расход: в 9.5k, из 124, кэш 2.8k · $0.0010" in text  # usage of the step with money
    assert "\x1b[" not in text  # not a terminal — no colors


def test_agy_sample(tmp_path, agy):
    log = copy_sample("agy/tools.ndjson", tmp_path, [
        {"ts": 1790776503000, "turn": 1, "kind": "start", "text": "Создай out.txt с hello."},
    ])
    items = items_of(agy(), log)
    assert [(i.kind, i.tool) for i in items if i.kind == "tool"] == [("tool", "write_to_file")]
    result = next(i for i in items if i.kind == "result")
    assert result.args == "/tmp/opencode/agytest/out.txt" and result.status == "DONE"
    assert all(i.turn == 1 for i in items)  # the run starts with init.conversation_id → turn 1

    text = render(agy(), log)
    assert "✎ write_to_file  /tmp/opencode/agytest/out.txt" in text
    assert "  ✔ done" in text
    # agy repeats a growing text buffer on every delta (and the result repeats it once more)
    assert text.count("Created\n") == 1 and text.count("with the content `hello`.") == 1
    assert "расход: в 25.2k, из 99, кэш 0" in text  # agy reports no prices — no money in the line


def test_codex_sample(tmp_path, codex):
    log = copy_sample("codex/commands.ndjson", tmp_path, [
        {"ts": 1790776503000, "turn": 1, "kind": "start", "text": "Создай sbox.txt."},
    ])
    items = items_of(codex(), log)
    tool = next(i for i in items if i.kind == "tool")
    assert tool.tool == "command_execution" and tool.args == "echo hello > sbox.txt"  # the shell wrapper is out
    text = render(codex(), log)
    assert "▶ command_execution  echo hello > sbox.txt" in text
    assert "I’ll create the requested file now." in text and "DONE" in text


def test_codex_tool_output_is_shown(tmp_path, codex):
    """codex reports what a command printed — the transcript shows the first lines of it."""
    log = tmp_path / "executor.log"
    log.write_text(json.dumps({"type": "item.completed", "item": {
        "id": "item_1", "type": "command_execution", "command": "/bin/bash -lc 'pytest -q'",
        "aggregated_output": "3 failed\n" + "detail line\n" * 10, "exit_code": 1, "status": "failed"}}) + "\n",
        encoding="utf-8")
    text = render(codex(), log)
    assert "▶ command_execution  pytest -q" in text  # the shell wrapper is out
    assert "  ✖ failed" in text
    assert "    3 failed" in text
    assert "ещё 5 строк" in text  # the tail of a long output is cut


def test_prompt_cut_unless_full(tmp_path, codex):
    log = copy_sample("codex/hello.ndjson", tmp_path, [
        {"ts": 1790776503000, "turn": 1, "kind": "start",
         "text": "\n".join(f"строка {i}" for i in range(1, 31))},
    ])
    short = render(codex(), log)
    assert "  строка 20" in short and "строка 21" not in short
    assert "ещё 10 строк" in short
    full = render(codex(), log, full=True)
    assert "  строка 30" in full and "ещё" not in full


def test_colors_only_when_asked(tmp_path, codex):
    log = copy_sample("codex/hello.ndjson", tmp_path, [
        {"ts": 1790776503000, "turn": 1, "kind": "start", "text": "Создай sbox.txt."},
    ])
    assert "\x1b[2m" in render(codex(), log, color=True)
    assert transcript.dim("x", True) == "\x1b[2mx\x1b[0m" and transcript.dim("x") == "x"


def test_reader_is_incremental(tmp_path, codex):
    log = copy_sample("codex/hello.ndjson", tmp_path, [
        {"ts": 1790776503000, "turn": 1, "kind": "start", "text": "Создай sbox.txt."},
    ])
    reader = transcript.Reader(codex(), log)
    first = reader.read()
    assert first.prompts and first.items and not reader.read()  # nothing new
    with transcript.prompts_path(log).open("a", encoding="utf-8") as f:  # the next turn of the same session
        f.write(json.dumps({"ts": 1790776560000, "turn": 2, "kind": "continue",
                            "text": "Продолжай."}) + "\n")
    with log.open("a", encoding="utf-8") as f:  # a new run of codex starts with thread.started
        f.write(json.dumps({"type": "thread.started", "thread_id": "th-2"}) + "\n")
        f.write(json.dumps({"type": "item.completed",
                            "item": {"id": "item_1", "type": "agent_message", "text": "готово"}}) + "\n")
    second = reader.read()
    assert [p.turn for p in second.prompts] == [2] and [(i.kind, i.turn) for i in second.items] == [("text", 2)]
    out = io.StringIO()
    writer = transcript.Writer(out, color=False)
    writer.write(first)
    writer.write(second)
    assert "── Turn 1 · start · " in out.getvalue() and "── Turn 2 · continue · " in out.getvalue()
    assert "готово" in out.getvalue()


def test_reader_without_prompts(tmp_path, codex):
    """A session of an old task: no sidecar — the stream is shown without turn headers."""
    log = copy_sample("codex/hello.ndjson", tmp_path)
    assert transcript.read_prompts(transcript.prompts_path(log)) == []
    text = render(codex(), log)
    assert "Turn" not in text and "OK" in text


def test_broken_lines_are_skipped(tmp_path, codex):
    log = copy_sample("codex/hello.ndjson", tmp_path)
    with transcript.prompts_path(log).open("w", encoding="utf-8") as f:
        f.write("не json\n" + json.dumps({"ts": 1, "turn": 1, "kind": "start", "text": "почини"}) + "\n{}\n")
    with log.open("a", encoding="utf-8") as f:
        f.write("мусор\n")
    text = render(codex(), log)
    assert "── Turn 1 · start · " in text and "OK" in text


def test_prompts_path_and_kinds():
    assert str(transcript.prompts_path("/w/.ahub/logs/executor.log")).endswith("executor.log.prompts.jsonl")
    assert transcript.PROMPT_KINDS == ("start", "continue", "repair", "rework", "stop", "review", "nudge")


# --- the engine writes the sidecar ---


def test_engine_saves_the_prompt_of_every_turn(tmp_path):
    store = Store()
    project = make_project(tmp_path)
    install_fake(store, [{"session": "ses_r", "steps": [{"event": {"type": "text", "text": "всё"}}]},  # no result
                         scout_ok("ses_r")])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="где утечка", model="fake"),
                     project, collect=False)
    assert Engine(store, project, t.id, sleep=lambda s: None).run().state is State.DONE
    side = transcript.prompts_path(Path(store.get_task(t.id).worktree) / ".ahub" / "logs" / "scout.log")
    lines = [json.loads(ln) for ln in side.read_text(encoding="utf-8").splitlines()]
    assert [(p["turn"], p["kind"]) for p in lines] == [(1, "start"), (2, "repair")]
    assert "где утечка" in lines[0]["text"] and "Result is not in the required form" in lines[1]["text"]
    assert all(p["ts"] > 0 and p["text"] for p in lines)
    # the transcript of the same session (fake provider) shows both turns and what the worker did
    text = render(providers.get("fake"), side.parent / "scout.log")
    assert "── Turn 1 · start · " in text and "── Turn 2 · repair · " in text
    assert "готово" in text and "👁 read" in text and "расход: в 100" in text


def test_engine_retry_does_not_add_a_turn(tmp_path):
    """A network retry repeats the run of the same prompt — one line in the sidecar, one Turn."""
    store = Store()
    project = make_project(tmp_path)
    install_fake(store, [{"session": "ses_t", "steps": [{"event": {"type": "error", "message": "status 503"}}],
                          "exit": 1},
                         scout_ok("ses_t")])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="где утечка", model="fake"),
                     project, collect=False)
    assert Engine(store, project, t.id, sleep=lambda s: None).run().state is State.DONE
    side = transcript.prompts_path(Path(store.get_task(t.id).worktree) / ".ahub" / "logs" / "scout.log")
    assert [(p.turn, p.kind) for p in transcript.read_prompts(side)] == [(1, "start")]
    text = render(providers.get("fake"), side.parent / "scout.log")
    assert text.count("── Turn ") == 1 and "✖ status 503" in text and "готово" in text


def test_engine_prompt_kinds_of_a_code_task(tmp_path):
    """One sidecar per session log, one line per turn: start, rework on the findings, review."""
    store = Store()
    project = make_project(tmp_path)
    high = [{"severity": "high", "file": "core/b.py", "line": 1, "issue": "Y должен быть 3",
             "fix": "поставить 3"}]

    def work(text: str = "Y = 2\n") -> dict:
        return {"session": "ses_x", "steps": [
            {"write": {"path": "core/b.py", "text": text}}, {"git_commit": "feat: сделал"},
            {"result": {"summary": "сделал", "files": ["core/b.py"]}}]}

    def verdict(round_no: int, v: str = "approve") -> dict:
        found = high if v != "approve" else []
        return {"session": f"ses_rev{round_no}", "steps": [
            {"write": {"path": f".ahub/review_r{round_no}_fake.json",
                       "text": json.dumps({"verdict": v, "summary": "ок", "findings": found})}}]}

    install_fake(store, [work(), verdict(1, "changes"), work("Y = 3\n"), verdict(2)])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.CODE, title="починить", model="fake",
                                           paths=["core/**", "tests/**"], accept=["tests/test_a.py::test_x"],
                                           review_level=0, review_models=["fake"], review_rounds=2),
                     project, collect=False)
    assert Engine(store, project, t.id, sleep=lambda s: None).run().state is State.DONE
    logs = Path(store.get_task(t.id).worktree) / ".ahub" / "logs"

    def kinds(name: str) -> list[tuple[int, str]]:
        return [(p.turn, p.kind) for p in transcript.read_prompts(transcript.prompts_path(logs / name))]

    assert kinds("executor.log") == [(1, "start"), (2, "rework")]
    assert kinds("reviewer_r1_fake.log") == [(1, "review")]
    assert kinds("reviewer_r2_fake.log") == [(1, "review")]
    rework = transcript.read_prompts(transcript.prompts_path(logs / "executor.log"))[1]
    assert "Y должен быть 3" in rework.text  # the findings of the panel


# --- `ahub follow` ---


def make_session(store: Store, task_id: int, log: Path, *, role: str = "executor", round_no: int = 1,
                 provider: str = "fake", status: str = "ok") -> None:
    row = store.add_session(task_id=task_id, provider=provider, role=role, model="fake", round=round_no,
                            external_id=f"ses_{log.stem}", log_path=str(log))
    store.update_session(row, status=status, ended_at=1 if status != "running" else None)


def test_follow_prints_the_last_session(capsys, tmp_path):
    store = Store()
    project = make_project(tmp_path)
    install_fake(store, [scout_ok()])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="где утечка", model="fake"),
                     project, collect=False)
    assert Engine(store, project, t.id, sleep=lambda s: None).run().state is State.DONE
    rc = cli.main(["follow", t.label, "--no-follow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert t.label in out and "scout · fake" in out  # role and model of the session
    assert "── Turn 1 · start · " in out and "готово" in out
    assert "\x1b[" not in out  # capsys is not a terminal


def test_follow_no_follow_exits_without_an_active_task(capsys, tmp_path):
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="x", executor="fake")
    log = tmp_path / "executor.log"
    log.write_text(json.dumps({"type": "text", "text": "привет"}) + "\n", encoding="utf-8")
    make_session(store, tid, log, status="ok")
    assert cli.main(["follow", f"T{tid}", "--no-follow"]) == 0
    out = capsys.readouterr().out
    assert "привет" in out and "── Turn" not in out  # no sidecar — no turn headers
    assert cli.main(["follow", f"T{tid}"]) == 0  # a task that is not active: print once and exit
    assert "привет" in capsys.readouterr().out


def test_follow_stops_when_the_session_ends(capsys, tmp_path, monkeypatch):
    """A running task: follow keeps printing and stops by itself when the task is done."""
    import os

    from ahub.commands import follow

    monkeypatch.setattr(follow, "POLL_S", 0.05)
    store = Store()
    project = make_project(tmp_path)
    install_fake(store, [{"session": "ses_r", "steps": [{"event": {"type": "text", "text": "всё"}}]},  # no result
                         scout_ok("ses_r")])
    t = tasks.create(store, tasks.TaskSpec(project="P", kind=Kind.SCOUT, title="где утечка", model="fake"),
                     project, collect=False)
    # the engine runs in a thread of this process: follow sees its owner as a live task process
    monkeypatch.setattr(follow, "live_workers", lambda: {t.id: os.getpid()})
    box: dict = {}
    worker = threading.Thread(target=lambda: box.setdefault(
        "r", Engine(store, project, t.id, sleep=lambda s: None).run()))
    worker.start()
    for _ in range(100):
        if store.list_sessions(t.id):
            break
        time.sleep(0.05)
    started = time.monotonic()
    rc = cli.main(["follow", t.label])
    took = time.monotonic() - started
    worker.join(60)
    assert rc == 0 and box["r"].state is State.DONE
    assert took < 60  # it stopped by itself when the task left the active state
    out = capsys.readouterr().out
    assert "── Turn 1 · start · " in out and "── Turn 2 · repair · " in out
    assert "follow закончился: сессия — ok" in out


def test_follow_stops_when_the_owner_is_gone(capsys, tmp_path, monkeypatch):
    """A worker that died leaves the task active — follow must not hang on it."""
    from ahub.commands import follow

    monkeypatch.setattr(follow, "POLL_S", 0.05)
    monkeypatch.setattr(follow, "live_workers", lambda: {})
    store = Store()
    tid = store.create_task(project="P", kind="code", title="x", executor="fake")
    transitions.move(store, tid, State.PREPARING)
    transitions.move(store, tid, State.WORKING)  # a task stuck active: its worker died
    log = tmp_path / "executor.log"
    log.write_text(json.dumps({"type": "text", "text": "начали"}) + "\n", encoding="utf-8")
    make_session(store, tid, log, status="running")
    store.update_session(store.list_sessions(tid)[-1].id, status="ok", ended_at=1)  # the turn is over
    started = time.monotonic()
    assert cli.main(["follow", f"T{tid}"]) == 0
    assert time.monotonic() - started < 30
    out = capsys.readouterr().out
    assert "начали" in out and "follow закончился" in out


def test_follow_role_and_round_filters(capsys, tmp_path):
    store = Store()
    tid = store.create_task(project="P", kind="code", title="x", executor="fake")
    ex = tmp_path / "executor.log"
    rv = tmp_path / "reviewer_r1_fake.log"
    for p in (ex, rv):
        p.write_text("", encoding="utf-8")
    make_session(store, tid, ex)
    make_session(store, tid, rv, role="reviewer")
    assert cli.main(["follow", f"T{tid}", "--role", "reviewer"]) == 0
    assert "reviewer" in capsys.readouterr().out
    assert cli.main(["follow", f"T{tid}", "--role", "executor"]) == 0
    assert "executor" in capsys.readouterr().out
    assert cli.main(["follow", f"T{tid}", "--role", "executor", "--round", "3"]) == 2
    assert "сессий пока нет" in capsys.readouterr().err


def test_follow_gone_worktree_names_the_archive(capsys, tmp_path, monkeypatch):
    project = make_project(tmp_path)
    monkeypatch.setattr("ahub.worker.find_project", lambda name: project)  # the project is in the hub config
    store = Store()
    tid = store.create_task(project="P", kind="code", title="x", executor="fake")
    store.update_task(tid, worktree=str(tmp_path / "gone"))
    make_session(store, tid, tmp_path / "gone" / ".ahub" / "logs" / "executor.log")
    assert cli.main(["follow", f"T{tid}"]) == 2
    err = capsys.readouterr().err
    assert "архив" in err and f".agent-hub/tasks/T{tid}" in err


def test_follow_without_sessions(capsys, tmp_path):
    store = Store()
    tid = store.create_task(project="P", kind="scout", title="x", executor="fake")
    assert cli.main(["follow", f"T{tid}"]) == 2
    assert "сессий пока нет" in capsys.readouterr().err
