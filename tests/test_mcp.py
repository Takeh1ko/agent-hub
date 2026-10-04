from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from ahub import comms, mcp
from ahub.store import Store
from tests.conftest import write


@pytest.fixture(autouse=True)
def _outside_every_project(monkeypatch, tmp_path):
    """These tests are not about the scope: the server is one started outside every project (the owner).
    The CLI re-resolves the scope from the directory, so the tools run there too."""
    monkeypatch.chdir(tmp_path)


def rpc(lines):
    out = io.StringIO()
    mcp.serve(io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n"), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_initialize_and_list():
    r = rpc([{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
             {"jsonrpc": "2.0", "method": "notifications/initialized"},
             {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}])
    assert len(r) == 2 and r[0]["result"]["serverInfo"]["name"] == "ahub"
    names = {t["name"] for t in r[1]["result"]["tools"]}
    assert {"task_new", "status", "result", "decide", "wait", "inbox", "say", "ask", "budget", "nudge"} <= names


def test_call_status_and_say():
    store = Store()
    r = rpc([{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "say",
                                                                           "arguments": {"text": "привет"}}}])
    assert r[0]["result"]["content"][0]["text"].startswith("тихо")
    assert not r[1]["result"]["isError"] and comms.outbox(store)[0]["text"] == "привет"


def test_call_nudge_refuses_a_queued_task():
    """The nudge tool is the same handle as the CLI: one line, code 2, nothing written."""
    store = Store()
    tid = store.create_task(project="P", kind="code", title="починить")
    r = rpc([{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "nudge", "arguments": {"task": f"T{tid}", "text": "почини"}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "nudge", "arguments": {"task": f"T{tid}"}}}])
    assert r[0]["result"]["isError"] and "написать ему некого" in r[0]["result"]["content"][0]["text"]
    assert r[1]["result"]["isError"] and "missing params: text" in r[1]["result"]["content"][0]["text"]
    assert store.get_task(tid).request == ""  # a refusal writes nothing


def test_budget_tool_lowers_the_real_money_budget():
    """The tool is the same handle as the CLI: it lowers the real budget too, and `add` is not required."""
    store = Store()
    tid = store.create_task(project="P", kind="code", title="починить")
    r = rpc([{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "budget", "arguments": {"task": f"T{tid}", "set_usd": 0.25}}}])
    assert not r[0]["result"]["isError"] and "реальные $0 → $0.25" in r[0]["result"]["content"][0]["text"]
    assert store.get_task(tid).budget_usd == 0.25


def test_inbox_tool_reads_the_messages_in_full():
    """An agent gets the whole text of every unread message — not the head a table cell holds."""
    store = Store()
    long = ("поручил " * 40).strip()
    comms.owner_message(store, long, project="P")
    r = rpc([{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "inbox", "arguments": {}}}])
    text = r[0]["result"]["content"][0]["text"]
    assert not r[0]["result"]["isError"] and long in " ".join(text.split())
    assert "ahub inbox" not in text  # nothing is cut, so no hint
    assert comms.inbox(store, mark=False) == []  # the tool read the inbox


def test_errors():
    r = rpc([{"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}},
             {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "result", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "result",
                                                                           "arguments": {"task": "T99"}}},
             {"jsonrpc": "2.0", "id": 8, "method": "bogus"}])
    assert r[0]["error"]["code"] == -32602
    assert r[1]["result"]["isError"] and "missing params" in r[1]["result"]["content"][0]["text"]
    assert r[2]["result"]["isError"] and "нет задачи" in r[2]["result"]["content"][0]["text"]
    assert r[3]["error"]["code"] == -32601


def test_stdio_process():
    p = subprocess.run([sys.executable, "-m", "ahub", "mcp"], input=json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n", capture_output=True, text=True, timeout=60)
    assert json.loads(p.stdout.splitlines()[0])["result"] == {}


def test_every_tool_takes_the_scope_of_the_directory_of_the_server(tmp_path, monkeypatch):
    """No list of scoped commands in the server: every tool is the CLI command, and the CLI resolves the
    scope from the directory the server runs in. A new tool is scoped from its first day."""
    root = tmp_path / "A"
    write(root / ".hub.toml", 'schema_version = 2\nname = "A"\n')
    monkeypatch.chdir(root)
    store = Store()
    comms.owner_message(store, "дело A", project="A")
    comms.owner_message(store, "дело B", project="B")
    comms.say(store, "сказано из A", project="A")
    b_task = store.create_task(project="B", kind="code", title="работа B")

    # the tools that read a scope
    inbox = call("inbox", {})["content"][0]["text"]
    assert "дело A" in inbox and "дело B" not in inbox
    assert f"T{b_task}" not in call("status", {})["content"][0]["text"]  # B's task is not A's
    assert "нет задачи T99" in call("result", {"task": "T99"})["content"][0]["text"]
    # and the one that writes into it
    call("say", {"text": "ещё одно"})
    assert [m["project"] for m in comms.outbox(store)] == ["A", "A"]


def call(name, args):
    out = io.StringIO()
    req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
    mcp.serve(io.StringIO(json.dumps(req) + "\n"), out)
    return json.loads(out.getvalue())["result"]
