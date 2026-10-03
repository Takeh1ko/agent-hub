"""Per-project scope: one hub, two projects — an orchestrator sees only its own.

Rows with project='' (hub-wide: observer alarms, service events) belong to everyone; acknowledgement is per
scope — `ack` in A never marks the events of B read; the MCP server takes the scope of its own cwd.
"""

from __future__ import annotations

import io
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ahub import cli, comms, events, mcp, paths, scope, transitions
from ahub.model import Ev, State
from ahub.scope import OWNER, Scope
from ahub.store import MIGRATIONS_DIR, Store, _split_sql
from tests.conftest import write


@pytest.fixture
def two_projects(tmp_path, monkeypatch):
    """Projects A and B in one hub; the current directory — A. Returns (store, root_a, root_b)."""
    roots = {}
    for name in ("A", "B"):
        root = tmp_path / name.lower()
        write(root / ".hub.toml", f'schema_version = 2\nname = "{name}"\nmax_parallel = 2\n')
        roots[name] = root
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text(f'projects = ["{roots["A"]}", "{roots["B"]}"]\n', encoding="utf-8")
    monkeypatch.chdir(roots["A"])
    return Store(), roots["A"], roots["B"]


def ahub(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out.strip(), out.err.strip()


def _talk(store: Store) -> None:
    """One owner message per project and one hub-wide — every one of them wakes the orchestrator."""
    comms.owner_message(store, "дело A", project="A")
    comms.owner_message(store, "дело B", project="B")
    comms.owner_message(store, "для всех", project="")


def test_resolve_by_flag_directory_and_nothing(two_projects, monkeypatch):
    _store, root_a, _root_b = two_projects
    assert scope.resolve(SimpleNamespace(all=False, project=None)) == Scope(("A",))
    assert scope.resolve(SimpleNamespace(all=True, project="B")) == OWNER  # --all wins
    assert scope.resolve(SimpleNamespace(all=False, project="B")) == Scope(("B",))
    assert scope.resolve(SimpleNamespace(all=False, project=str(root_a))) == Scope(("A",))  # a path works too
    sub = root_a / "sub" / "dir"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)  # a subdirectory — the project is searched upward
    assert scope.resolve(SimpleNamespace(all=False, project=None)) == Scope(("A",))
    monkeypatch.chdir(root_a.parent)  # outside every project — the owner sees everything
    assert scope.resolve(SimpleNamespace(all=False, project=None)) == OWNER
    assert scope.resolve() == OWNER  # no args at all (the MCP server)


def test_scope_sql_and_membership():
    assert scope.where(OWNER) == ("", [])
    assert scope.where(None) == ("", [])
    assert scope.where(Scope(("A",))) == ("(project IN (?) OR project='')", ["A"])
    assert scope.where(Scope(("A", "B")), "task.project")[1] == ["A", "B"]
    assert "" in Scope(("A",)) and "A" in Scope(("A",)) and "B" not in Scope(("A",))
    assert "B" in OWNER and Scope(("A", "B")).name == ""  # several projects — nothing to write on a row


def test_a_question_event_and_message_of_b_are_invisible_from_a(two_projects, capsys, monkeypatch):
    store, root_a, root_b = two_projects
    _talk(store)
    comms.ask(store, "вопрос A?", project="A")
    comms.ask(store, "вопрос B?", project="B")
    monkeypatch.chdir(root_a)

    rc, out, err = ahub(capsys, "wait", "--timeout", "1s")
    assert rc == 0 and "дело A" in out and "для всех" in out and "дело B" not in out, err
    questions = ahub(capsys, "questions")[1]
    assert "вопрос A?" in questions and "вопрос B?" not in questions
    inbox = ahub(capsys, "inbox")[1]
    assert "дело A" in inbox and "для всех" in inbox and "дело B" not in inbox

    monkeypatch.chdir(root_b)
    out = ahub(capsys, "wait", "--timeout", "1s")[1]
    assert "дело B" in out and "дело A" not in out
    assert "вопрос B?" in ahub(capsys, "questions")[1] and "вопрос A?" not in ahub(capsys, "questions")[1]


def test_all_and_project_flags_see_everything(two_projects, capsys):
    store, _root_a, _root_b = two_projects
    _talk(store)
    comms.ask(store, "вопрос A?", project="A")
    comms.ask(store, "вопрос B?", project="B")

    rc, out, _ = ahub(capsys, "--json", "wait", "--timeout", "1s", "--all")
    assert rc == 0 and len(json.loads(out)["events"]) == 3  # the owner: every project
    assert len(ahub(capsys, "questions", "--all")[1].splitlines()) == 3  # the column head + two questions
    assert "вопрос B?" in ahub(capsys, "questions", "--project", "B")[1]
    assert "вопрос A?" in ahub(capsys, "questions", "--project", "B", "--all")[1]


def test_watch_streams_only_the_events_of_the_scope(two_projects, capsys, monkeypatch):
    import time

    store, root_a, root_b = two_projects
    monkeypatch.setattr(time, "sleep", lambda s: (_ for _ in ()).throw(KeyboardInterrupt()))
    _talk(store)
    events.mark_delivered(store, [e.id for e in store.events(needs_reaction=True)])

    out = ahub(capsys, "watch", "--poll", "0")[1]
    assert "НЕПРОЧИТАНО 2" in out and "дело A" in out and "дело B" not in out
    monkeypatch.chdir(root_b)
    out = ahub(capsys, "watch", "--poll", "0")[1]
    assert "НЕПРОЧИТАНО 2" in out and "дело B" in out and "дело A" not in out
    assert store.meta_get("watch_summary:claude:A") and store.meta_get("watch_summary:claude:B")
    assert ahub(capsys, "watch", "--poll", "0")[1] == ""  # A announced its own already


def test_ack_in_a_leaves_b_unread(two_projects, capsys):
    store, _root_a, _root_b = two_projects
    _talk(store)
    assert ahub(capsys, "ack", "all")[1] == "подтверждено 2"  # A and the hub-wide one, not B
    assert [e.project for e in events.unacked(store)] == ["B"]
    assert events.ack(store, scope=OWNER) == 1
    assert events.unacked(store) == []


def test_ack_by_number_stays_inside_the_scope(two_projects, capsys):
    store, _root_a, _root_b = two_projects
    _talk(store)
    ids = {e.payload.get("text"): e.id for e in store.events(needs_reaction=True)}
    assert ahub(capsys, "ack", str(ids["дело B"]))[1] == "подтверждено 0"
    assert len(events.unacked(store, OWNER)) == 3  # B's event is not read from A
    assert ahub(capsys, "ack", str(ids["дело A"]))[1] == "подтверждено 1"


def test_inbox_reads_only_its_scope(two_projects, capsys):
    store, _root_a, _root_b = two_projects
    _talk(store)
    assert "дело B" not in ahub(capsys, "inbox")[1]
    assert len(comms.inbox(store, mark=False, scope=OWNER)) == 1  # B's message is still unread
    assert len(comms.inbox(store, mark=False, scope=OWNER)) == 1  # and the hub-wide one was read


def test_hub_wide_alarms_are_seen_by_every_scope(two_projects, capsys):
    store, _root_a, _root_b = two_projects
    comms.raise_alarm(store, "opencode недоступен", critical=True)  # the observer writes project=''
    comms.raise_alarm(store, "диск полон", project="B")
    out = ahub(capsys, "alarms")[1]
    assert "opencode недоступен" in out and "диск полон" not in out
    out = ahub(capsys, "alarms", "--project", "B")[1]
    assert "диск полон" in out and "opencode недоступен" in out
    assert "диск полон" in ahub(capsys, "alarms", "--all", "--acked")[1]


def test_status_and_history_show_only_the_scope(two_projects, capsys):
    store, _root_a, _root_b = two_projects
    working = {}
    for name in ("A", "B"):
        working[name] = store.create_task(project=name, kind="scout", title=f"работа {name}")
        transitions.move(store, working[name], State.PREPARING, now=0)
        transitions.move(store, working[name], State.WORKING, now=0)
    done = store.create_task(project="B", kind="scout", title="сделано B")
    transitions.move(store, done, State.REJECTED, now=0)
    comms.ask(store, "вопрос B?", project="B")
    comms.owner_message(store, "сообщение B", project="B")
    comms.owner_message(store, "для всех", project="")

    out = ahub(capsys, "status")[1]
    assert out.startswith("A · ") and "работа A" in out and "работа B" not in out
    assert "вопросов владельцу" not in out  # B's question is not A's
    assert "сообщений владельца 1" in out and "непрочитано событий 1" in out  # only the hub-wide message
    assert ahub(capsys, "history")[1] == "истории нет"
    out = ahub(capsys, "--json", "status", "--project", "B")[1]
    assert [t["id"] for t in json.loads(out)["active"]] == [working["B"]]
    out = ahub(capsys, "status", "--project", "B")[1]
    assert out.startswith("B · ") and "работа B" in out and "работа A" not in out
    assert "вопросов владельцу открыто 1" in out and "сообщений владельца 2" in out  # B's + hub-wide
    assert "сделано B" in ahub(capsys, "history", "--project", "B")[1]


def test_say_and_ask_write_the_current_project(two_projects, capsys, monkeypatch):
    store, root_a, root_b = two_projects
    monkeypatch.chdir(root_b)
    ahub(capsys, "say", "T1 готов")
    qid = json.loads(ahub(capsys, "--json", "ask", "сливать?", "--options", "да,нет")[1])["id"]
    assert [m["project"] for m in comms.outbox(store)] == ["B"]
    assert comms.open_questions(store)[0]["project"] == "B"

    tid = store.create_task(project="A", kind="code", title="кнопка")
    comms.ask(store, "продолжать?", task_id=tid)  # no project given — the task's one
    rows = {r["id"]: r["project"] for r in comms.open_questions(store, scope=OWNER)}
    assert rows == {qid: "B", qid + 1: "A"}

    assert comms.answer(store, qid, "да")  # the ANSWER event carries the question's project
    answer = [e for e in store.events(needs_reaction=True) if e.kind == Ev.ANSWER.value][0]
    assert answer.project == "B"
    assert [e.kind for e in events.unacked(store, Scope(("A",)))] == []


def test_question_project_is_backfilled_from_the_task(tmp_path):
    """Migration 005: an old row takes the project of its task; a question without a task stays hub-wide."""
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    for num in range(1, 5):
        f = next(p for p in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql")) if int(p.name[:3]) == num)
        for stmt in _split_sql(f.read_text(encoding="utf-8")):
            con.execute(stmt)
    con.execute("PRAGMA user_version=4")
    con.execute("INSERT INTO task(id, project, kind, title, created_at, updated_at)"
                " VALUES(7, 'B', 'scout', 'старая', 0, 0)")
    con.execute("INSERT INTO question(ts, task_id, asked_by, text) VALUES(0, 7, 'orchestrator', 'с задачей')")
    con.execute("INSERT INTO question(ts, task_id, asked_by, text) VALUES(1, NULL, 'hub', 'без задачи')")
    con.commit()
    con.close()

    store = Store(db)
    assert store.schema_version() == 5
    with store.read() as c:
        rows = c.execute("SELECT text, project FROM question ORDER BY id").fetchall()
    assert [(r["text"], r["project"]) for r in rows] == [("с задачей", "B"), ("без задачи", "")]


def test_mcp_takes_the_scope_of_its_cwd(two_projects, monkeypatch):
    store, root_a, root_b = two_projects
    _talk(store)
    monkeypatch.setattr(mcp, "_server_scope", None)  # resolved once, from the cwd of the server
    monkeypatch.chdir(root_a)
    assert mcp.server_scope() == Scope(("A",))

    def call(name, args):
        out = io.StringIO()
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        mcp.serve(io.StringIO(json.dumps(req) + "\n"), out)
        return json.loads(out.getvalue())["result"]["content"][0]["text"]

    assert "дело A" in call("inbox", {}) and "дело B" not in call("inbox", {})
    assert "дело B" in call("inbox", {"project": "B"})  # the call may ask for another project
    call("say", {"text": "привет из MCP"})
    assert [m["project"] for m in comms.outbox(store)] == ["A"]
    monkeypatch.chdir(root_b)
    mcp._server_scope = None
    assert mcp.server_scope() == Scope(("B",))
