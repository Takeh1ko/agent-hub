from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from ahub import comms, mcp
from ahub.scope import OWNER
from ahub.store import Store


@pytest.fixture(autouse=True)
def _owner_scope(monkeypatch, tmp_path):
    """These tests are not about the scope: the server is one started outside every project (the owner).
    The CLI re-resolves the scope from the directory, so the tools run there too."""
    monkeypatch.setattr(mcp, "_server_scope", OWNER)
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
