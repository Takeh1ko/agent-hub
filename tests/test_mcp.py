from __future__ import annotations

import io
import json
import subprocess
import sys

from ahub import mcp, comms
from ahub.store import Store


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
    assert {"task_new", "status", "result", "decide", "wait", "inbox", "say", "ask", "budget"} <= names


def test_call_status_and_say():
    store = Store()
    r = rpc([{"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
             {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "say",
                                                                           "arguments": {"text": "привет"}}}])
    assert r[0]["result"]["content"][0]["text"].startswith("тихо")
    assert not r[1]["result"]["isError"] and comms.outbox(store)[0]["text"] == "привет"


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
