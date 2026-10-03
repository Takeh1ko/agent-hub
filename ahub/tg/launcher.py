"""Claude Code launch from TG when no live session exists (V27, architecture §11; owner decision).

- Trigger: unread human messages, Claude not marking presence (wait/watch), nothing running.
- One Claude at a time: while one runs, new messages queue — it picks them up via `ahub inbox`
  before finishing, the rest goes to the next launch.
- Project: named in the message, else where Claude last worked (presence), else the hub's first project.
- One resumable "TG session" (claude --resume) within a day and while turns stay under MAX_TURNS.
- `--dangerously-skip-permissions` — same as the owner. Supervision: timeout, launch journal (claude_launch),
  hourly limit.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ahub import config, events, paths, procs, views
from ahub import log as hublog
from ahub.store import Store
from ahub.time import now_ms, to_local

SESSION_KEY = "tg_claude_session"
TIMEOUT_MS = 30 * 60_000
MAX_TURNS = 30
MAX_PER_HOUR = 6
MIN_ALIVE_MS = 20_000  # lived too briefly with no session — messages stay undelivered (a restart picks them up)
_log = hublog.get("launcher")


def _owner_lang_line() -> str:
    from ahub.i18n import lang

    language = "Russian" if lang() == "ru" else "English"
    return f"Write all messages to the owner in {language}."

PROMPT = """You are Claude, running development via agent-hub (`ahub` CLI). The owner is away from the computer
and writes to you from Telegram, like to a senior in the office. You have no live session — the hub started you.

Owner messages:
{messages}

Hub summary (ahub status):
{status}

How to work:
- Reply to the owner only via `ahub say "text"` (short, no markup). A question with options —
  `ahub ask "question" --options "yes,no"`; the answer arrives as an event (check `ahub wait --timeout 10m`).
- File and run tasks via `ahub` (`ahub --help`, `ahub task new --help`); task details — `ahub status T<id>`,
  result — `ahub result T<id>`. Do not write code yourself, unless it is a tiny fix on top of a task result.
- Merging code — only with the owner's consent (ask via `ahub ask`).
- Before finishing, check `ahub inbox` — new messages may have arrived.
{lang_line}
"""


def claude_bin() -> str | None:
    """claude binary: [paths].claude → which → ~/.claude/local/claude."""
    try:
        override = config.load_hub().claude
        if override:
            return override
    except config.ConfigError:
        pass
    return shutil.which("claude") or (str(Path.home() / ".claude/local/claude")
                                      if (Path.home() / ".claude/local/claude").exists() else None)


@dataclass
class LaunchState:
    id: int
    pid: int | None
    project: str
    ts: int
    session_id: str
    log: str


def running(store: Store) -> LaunchState | None:
    with store.read() as c:
        row = c.execute("SELECT * FROM claude_launch WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    log_path = str(paths.state_dir() / "claude" / f"launch_{row['id']}.log")
    return LaunchState(row["id"], row["pid"], row["project"], row["ts"], row["session_id"], log_path)


def _pending(store: Store) -> list[dict]:
    with store.read() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, ts, text, project FROM message WHERE direction='in' AND delivered_at IS NULL ORDER BY id")]


def _pick_project(store: Store, msgs: list[dict], projects: list[config.ProjectConfig]) -> config.ProjectConfig | None:
    by = {p.name: p for p in projects}
    for m in reversed(msgs):
        if m.get("project") in by:
            return by[m["project"]]
    pres = events.presence(store)
    if pres and pres.get("project") in by:
        return by[pres["project"]]
    return projects[0] if projects else None


def _session(store: Store, now: int) -> str | None:
    raw = store.meta_get(SESSION_KEY)
    if not raw:
        return None
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if s.get("day") != to_local(now).date().isoformat() or int(s.get("turns", 0)) >= MAX_TURNS:
        return None
    return s.get("id") or None


def _launches_last_hour(store: Store, now: int) -> int:
    with store.read() as c:
        return c.execute("SELECT COUNT(*) FROM claude_launch WHERE ts>?", (now - 3_600_000,)).fetchone()[0]


def _deliver(store: Store, message_ids: list[int], now: int) -> None:
    """Messages handed to Claude: marked read + their events acked."""
    with store.tx() as c:
        c.execute(f"UPDATE message SET delivered_at=? WHERE delivered_at IS NULL AND id IN "
                  f"({','.join('?' * len(message_ids))})", (now, *message_ids))
    for e in events.unacked(store):
        if e.kind == "owner_message" and e.payload.get("message_id") in message_ids:
            events.ack(store, [e.id], now=now)


def build_command(binary: str, prompt: str, resume: str | None) -> list[str]:
    cmd = [binary, "-p", prompt, "--dangerously-skip-permissions", "--output-format", "stream-json", "--verbose"]
    if resume:
        cmd += ["--resume", resume]
    return cmd


def session_id_from_log(path: str) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for ln in lines:
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("session_id"):
            return str(ev["session_id"])
    return ""


def tick(store: Store, *, projects: list[config.ProjectConfig] | None = None, now: int | None = None,
         spawn=None, binary: str | None = None) -> str:
    """One supervise/launch step. Returns what happened: idle | running | finished | killed | launched | limit."""
    ts = now if now is not None else now_ms()
    cur = running(store)
    if cur is not None:
        alive = procs.alive(cur.pid) if cur.pid else False
        if alive and ts - cur.ts < TIMEOUT_MS:
            return "running"
        status = "ok"
        if alive:
            try:
                os.killpg(os.getpgid(cur.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            status = "killed"
        sid = session_id_from_log(cur.log) or cur.session_id
        with store.tx() as c:
            c.execute("UPDATE claude_launch SET status=?, ended_at=?, session_id=? WHERE id=?",
                      (status, ts, sid, cur.id))
        ids = json.loads(store.meta_get(f"launch_msgs:{cur.id}") or "[]")
        if ids and (sid or ts - cur.ts >= MIN_ALIVE_MS):
            _deliver(store, ids, ts)
        store.meta_del(f"launch_msgs:{cur.id}")
        if sid:
            prev = {}
            try:
                prev = json.loads(store.meta_get(SESSION_KEY) or "{}")
            except json.JSONDecodeError:
                prev = {}
            turns = int(prev.get("turns", 0)) + 1 if prev.get("id") == sid else 1
            store.meta_set(SESSION_KEY, json.dumps({"id": sid, "day": to_local(ts).date().isoformat(),
                                                    "turns": turns}))
        _log.info("launched Claude finished: %s", status)
        return "killed" if status == "killed" else "finished"
    msgs = _pending(store)
    if not msgs or events.present(store, now=ts):
        return "idle"
    if _launches_last_hour(store, ts) >= MAX_PER_HOUR:
        return "limit"
    if projects is None:
        projects, _ = config.load_projects()
    project = _pick_project(store, msgs, projects)
    binary = binary or claude_bin()
    if project is None or binary is None:
        _log.error("cannot launch Claude: %s", "no project" if project is None else "no claude binary")
        return "idle"
    prompt = PROMPT.format(messages="\n".join(f"- {m['text']}" for m in msgs)[:6000],
                           status=views.status_text(store, now=ts),
                           lang_line=_owner_lang_line())
    resume = _session(store, ts)
    with store.tx() as c:
        lid = int(c.execute("INSERT INTO claude_launch(ts, project, session_id, reason) VALUES(?,?,?,?)",
                            (ts, project.name, resume or "", f"{len(msgs)} messages")).lastrowid)
    log_path = paths.state_dir() / "claude" / f"launch_{lid}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_command(binary, prompt, resume)
    if spawn is None:
        with open(log_path, "ab") as out:
            p = subprocess.Popen(cmd, cwd=project.root, stdout=out, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        pid = p.pid
    else:
        pid = spawn(cmd, project.root, str(log_path))
    with store.tx() as c:
        c.execute("UPDATE claude_launch SET pid=? WHERE id=?", (pid, lid))
    # messages count as delivered once the launched Claude has lived a while (see MIN_ALIVE_MS) — not at once
    store.meta_set(f"launch_msgs:{lid}", json.dumps([m["id"] for m in msgs]))
    _log.info("launched Claude in %s (pid %s, %s)", project.name, pid, "resume" if resume else "new session")
    return "launched"
