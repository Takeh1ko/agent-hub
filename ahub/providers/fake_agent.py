"""Fake agent for tests: a real process that plays a scenario from JSON.

Run: python -m ahub.providers.fake_agent <scenario-file> [--session ID]

Scenario: {"session": "ses_x", "steps": [...], "exit": 0}
Steps:
  {"event": {...}}                   — print a JSON event line (type: text|tool_start|tool_end|step|error|usage)
  {"sleep": 0.3}                     — pause
  {"child": 1.5}                     — run a child process for N seconds and wait for it (silence with a child)
  {"bg": 30, "detach": false}        — drop a background process and exit (detach — also leave the group via setsid)
  {"write": {"path": "a.txt", "text": "..."}}  — write a file into cwd
  {"git_commit": "message"}          — git add -A && git commit in cwd
  {"result": {...}}                  — .ahub/result.json with commit = current HEAD
  {"stderr": "text"}                 — a line to stderr
  {"crash": true}                    — exit at once with no result (code 137)
With --session, the session id = it (resume), otherwise session from the scenario.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def main(argv: list[str]) -> int:
    scenario = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    sid = scenario.get("session", "")
    if "--session" in argv:
        sid = argv[argv.index("--session") + 1]
    if sid and not scenario.get("hide_session"):
        print(json.dumps({"type": "session", "sessionID": sid}), flush=True)
    for step in scenario.get("steps", []):
        if "event" in step:
            ev = dict(step["event"])
            ev.setdefault("sessionID", sid)
            print(json.dumps(ev, ensure_ascii=False), flush=True)
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif "bg" in step:
            # Abandoned background process (like an agent's `yes > /dev/null &`); detach — also leave the group (setsid).
            subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({float(step['bg'])})"],
                             start_new_session=bool(step.get("detach")), stdout=subprocess.DEVNULL)
            time.sleep(float(step.get("settle", 2.5)))  # let the watchdog notice the descendant
        elif "child" in step:
            subprocess.run([sys.executable, "-c", f"import time; time.sleep({float(step['child'])})"])
        elif "write" in step:
            p = Path.cwd() / step["write"]["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(step["write"].get("text", ""), encoding="utf-8")
        elif "git_commit" in step:
            subprocess.run(["git", "add", "-A"], check=True, capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", step["git_commit"]], check=True, capture_output=True)
        elif "result" in step:
            # Worker result with the real HEAD (as a worker would): commit fills itself in.
            res = dict(step["result"])
            head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            res.setdefault("commit", head)
            res.setdefault("status", "done")
            p = Path.cwd() / ".ahub" / "result.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        elif "stderr" in step:
            print(step["stderr"], file=sys.stderr, flush=True)
        elif step.get("crash"):
            return 137
    return int(scenario.get("exit", 0))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
