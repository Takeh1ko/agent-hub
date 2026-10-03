"""ahub MCP server (M7, architecture §9): same handles as the CLI, as tools — for Codex and other agents.

Transport — stdio, line-delimited JSON-RPC 2.0 (MCP protocol 2025-06-18: initialize, tools/list, tools/call, ping).
No outside deps. Each tool calls a CLI handle in this process and returns its text
(same L0–L3 limits, same savings). Wiring: `ahub mcp` as a stdio server in agent settings.

Scope (ahub/scope.py): the server resolves the scope once from its own cwd at start, so its tools see and
write only that project; a call may pass `project` (or `all`) to look at another one. A tool on one task id belongs
to that task's project — a task of another project comes back as an error with the way out.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any

from ahub import __version__, scope

PROTOCOL = "2025-06-18"

# The commands that take a scope; the server passes its own one to them (ahub/scope.py). A command on one
# task is here too: a task of another project is refused unless the call names its project or asks for all.
SCOPED = frozenset({"status", "wait", "watch", "ack", "inbox", "say", "ask", "questions", "alarms", "history",
                    "result", "nudge", "budget", "accept", "reject", "rework", "continue", "stop"})

# name → (description, param schema, how to build CLI argv)
TOOLS: dict[str, tuple[str, dict, Any]] = {}

_server_scope: scope.Scope | None = None


def server_scope(cwd: str | Path | None = None) -> scope.Scope:
    """The scope of this server — the project of its cwd, resolved once (the cwd does not change)."""
    global _server_scope
    if _server_scope is None:
        _server_scope = scope.of_dir(Path(cwd) if cwd is not None else Path.cwd())
    return _server_scope


def _scoped(argv: list[str]) -> list[str]:
    """The server's scope on a scoped command, unless the call named a project or asked for everything."""
    if len(argv) < 2 or argv[0] not in SCOPED or "--project" in argv or "--all" in argv:
        return argv
    sc = server_scope()
    return argv[:1] + (["--project", sc.name] if sc.name else []) + argv[1:]


def tool(name: str, description: str, props: dict, required: list[str] | None = None):
    def deco(fn):
        TOOLS[name] = (description, {"type": "object", "properties": props, "required": required or []}, fn)
        return fn
    return deco


S = {"type": "string"}
I = {"type": "integer"}  # noqa: E741


@tool("task_new", "Create a task for a worker (scout|code|routine|review). Reply is one line with the number.",
      {"kind": {"type": "string", "enum": ["scout", "code", "routine", "review"]}, "title": S, "spec": S,
       "project": S, "paths": S, "accept": S, "level": I, "after": S, "budget": {"type": "number"}, "input": S,
       "model": S}, ["kind", "title"])
def _task_new(a: dict) -> list[str]:
    argv = ["task", "new", "--kind", a["kind"], "--title", a["title"]]
    for k in ("spec", "paths", "accept", "after", "input", "model"):
        if a.get(k):
            argv += [f"--{k}", str(a[k])]
    if a.get("project"):
        argv += ["--project", a["project"]]
    if a.get("level") is not None:
        argv += ["--level", str(a["level"])]
    if a.get("budget") is not None:
        argv += ["--budget", str(a["budget"])]
    return argv


def _project_flag(args: dict, argv: list[str]) -> list[str]:
    """A call may pass `project` (or `all`) to look at another project than the server's own."""
    if args.get("all"):
        return argv + ["--all"]
    if args.get("project"):
        return argv + ["--project", str(args["project"])]
    return argv


@tool("status", "Hub summary (<=1.5 KB) or a task (<=4 KB) when task is given (T12).",
      {"task": S, "project": S, "all": {"type": "boolean"}})
def _status(a: dict) -> list[str]:
    argv = ["status"] + ([a["task"]] if a.get("task") else [])
    return _project_flag(a, argv)


@tool("result", "Task result: short, or full=true for the full text.",
      {"task": S, "full": {"type": "boolean"}, "project": S, "all": {"type": "boolean"}}, ["task"])
def _result(a: dict) -> list[str]:
    return _project_flag(a, ["result", a["task"]] + (["--full"] if a.get("full") else []))


@tool("decide", "Decision on a task: accept | reject | rework (notes) | continue | stop.",
      {"task": S, "action": {"type": "string", "enum": ["accept", "reject", "rework", "continue", "stop"]},
       "notes": S, "reason": S, "project": S, "all": {"type": "boolean"}}, ["task", "action"])
def _decide(a: dict) -> list[str]:
    act = a["action"]
    argv = [act, a["task"]]
    if act == "rework":
        argv += ["--notes", a.get("notes") or "rework"]
    elif a.get("reason") and act in ("accept", "reject", "continue", "stop"):
        argv += ["--reason", a["reason"]]
    return _project_flag(a, argv + ["--by", "mcp"])


@tool("nudge", "Message a working agent in its own session (a stuck or off-track worker; prefer it over "
      "stop+continue).", {"task": S, "text": S, "project": S, "all": {"type": "boolean"}}, ["task", "text"])
def _nudge(a: dict) -> list[str]:
    return _project_flag(a, ["nudge", a["task"], a["text"], "--by", "mcp"])


@tool("wait", "Wait for orchestrator events (DONE/DECISION/ERROR/OWNER/ALARM lines).",
      {"timeout": S, "project": S, "all": {"type": "boolean"}})
def _wait(a: dict) -> list[str]:
    return _project_flag(a, ["wait", "--timeout", a.get("timeout") or "10m", "--who", "mcp"])


@tool("inbox", "Owner messages of the project in full (marked as read).",
      {"project": S, "all": {"type": "boolean"}, "peek": {"type": "boolean"}})
def _inbox(a: dict) -> list[str]:
    argv = ["inbox", "--full"] + (["--peek"] if a.get("peek") else [])  # an agent reads the whole text
    return _project_flag(a, argv)


@tool("say", "Write to the owner in Telegram.", {"text": S, "project": S}, ["text"])
def _say(a: dict) -> list[str]:
    return _project_flag(a, ["say", a["text"]])


@tool("ask", "Question to the owner with options; the answer arrives as an ANSWER event.",
      {"text": S, "options": S, "task": S, "project": S}, ["text"])
def _ask(a: dict) -> list[str]:
    argv = ["ask", a["text"]]
    if a.get("options"):
        argv += ["--options", a["options"]]
    if a.get("task"):
        argv += ["--task", a["task"]]
    return _project_flag(a, argv)


@tool("budget", "Extend the task budget (a budget-blocked task resumes).",
      {"task": S, "add": {"type": "number"}, "project": S, "all": {"type": "boolean"}}, ["task", "add"])
def _budget(a: dict) -> list[str]:
    return _project_flag(a, ["budget", a["task"], "--add", str(a["add"]), "--by", "mcp"])


def call_cli(argv: list[str]) -> tuple[int, str]:
    from ahub import cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = cli.main(argv)
        except SystemExit as e:  # argparse
            rc = int(e.code or 2)
    text = out.getvalue().strip()
    if err.getvalue().strip():
        text = (text + "\n" if text else "") + err.getvalue().strip()
    return rc, text


def handle(req: dict) -> dict | None:
    rid = req.get("id")
    method = req.get("method", "")
    if rid is None:  # notification (notifications/initialized etc.)
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                   "serverInfo": {"name": "ahub", "version": __version__}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": d, "inputSchema": s} for n, (d, s, _) in TOOLS.items()]})
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"no tool {name}"}}
        args = params.get("arguments") or {}
        missing = [k for k in TOOLS[name][1]["required"] if k not in args]
        if missing:
            return ok({"content": [{"type": "text", "text": f"missing params: {', '.join(missing)}"}], "isError": True})
        rc, text = call_cli(_scoped(TOOLS[name][2](args)))
        return ok({"content": [{"type": "text", "text": text or ("ok" if rc == 0 else f"code {rc}")}],
                   "isError": rc not in (0, 3)})
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown method {method}"}}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(req)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()
