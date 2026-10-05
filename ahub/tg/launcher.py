"""Claude Code launch from TG when no live session exists (V27, architecture §11; owner decision).

- Trigger: unread human messages of a project, no live session in that project (wait/watch presence), nothing
  the hub has started for it.
- Per project: pending owner messages are grouped by project; a project with no live session gets Claude in its
  own directory with its own messages only. A live session in A does not stop a launch for B.
  Hub-wide messages (project='') are for the owner — one launch of its own, never mixed into a project's prompt.
- One launched Claude per project: while one runs, new messages of that project queue — it picks them up via
  `ahub inbox` before finishing, the rest goes to the next launch.
- Directory of an owner launch: where Claude last worked (presence), else the hub's first project.
- One resumable "TG session" (claude --resume) per project within a day and while turns stay under MAX_TURNS.
- `--dangerously-skip-permissions` — same as the owner. Supervision: timeout, launch journal (claude_launch),
  hourly limit (MAX_PER_HOUR — still hub-wide: one launch per project spends it), and every group with no
  directory here is reported once (`nodir:<project>`, the bot tells the owner; an empty name — the hub-wide
  group with no configured project) instead of every tick.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ahub import config, events, paths, procs, ui, views
from ahub import log as hublog
from ahub.scope import Scope
from ahub.store import Store
from ahub.time import now_ms, to_local

SESSION_KEY = "tg_claude_session"
TIMEOUT_MS = 30 * 60_000
MAX_TURNS = 30
MAX_PER_HOUR = 6  # hub-wide — one launch per project spends it
MIN_ALIVE_MS = 20_000  # lived too briefly with no session — messages stay undelivered (a restart picks them up)
NO_DIR_MS = 15 * 60_000  # how often a project without a directory is reported again
_log = hublog.get("launcher")


def session_key(project: str = "") -> str:
    """The resumable TG session of a launch: one per project (a session in A knows nothing about B)."""
    return f"{SESSION_KEY}:{project}"


def _owner_lang_line() -> str:
    from ahub.i18n import lang

    language = "Russian" if lang() == "ru" else "English"
    return f"Write all messages to the owner in {language}."


PROMPT = """You are Claude, running development via agent-hub (`ahub` CLI). The owner is away from the computer
and writes to you from Telegram, like to a senior in the office. You have no live session — the hub started you.
{project_line}

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

OWNER_LINE = ("You work with every project of this hub: pass `--all` (or `--project <name>`) to the `ahub` commands "
              "that read everything — status, inbox, wait, history. The messages below are for all of them.")


def project_line(project: str) -> str:
    """The scope line of the prompt: one repository, or the whole hub (a hub-wide launch)."""
    if project:
        return (f"Project: {project} — this repository only; the `ahub` commands here see {project} and "
                f"nothing else (no --all: another project's tasks are not yours).")
    return OWNER_LINE


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


def running(store: Store, project: str | None = None) -> list[LaunchState]:
    """Every launch the hub started that is still marked running; project — of one project only."""
    sql = "SELECT * FROM claude_launch WHERE status='running'"
    args: list = []
    if project is not None:
        sql += " AND project=?"
        args.append(project)
    with store.read() as c:
        rows = list(c.execute(sql + " ORDER BY id DESC", args))
    return [LaunchState(r["id"], r["pid"], r["project"], r["ts"], r["session_id"],
                        str(paths.state_dir() / "claude" / f"launch_{r['id']}.log")) for r in rows]


def _pending(store: Store) -> list[dict]:
    with store.read() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, ts, text, project FROM message WHERE direction='in' AND delivered_at IS NULL ORDER BY id")]


def _groups(store: Store) -> list[tuple[str, list[dict]]]:
    """Undelivered owner messages by project, oldest group first ('' — hub-wide, for the owner)."""
    groups: dict[str, list[dict]] = {}
    for m in _pending(store):
        groups.setdefault(m.get("project") or "", []).append(m)
    return list(groups.items())


def _root_for(target: str, projects: list[config.ProjectConfig], store: Store) -> Path | None:
    """Where Claude runs for a launch: the project's directory; an owner launch — where Claude last worked."""
    by = {p.name: p.root for p in projects}
    if target:
        return by.get(target)
    pres = events.presence(store)
    if pres and pres.get("project") in by:
        return by[pres["project"]]
    return projects[0].root if projects else None


def _session(store: Store, now: int, target: str) -> str | None:
    raw = store.meta_get(session_key(target))
    if not raw:
        return None
    try:
        s = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if s.get("day") != to_local(now).date().isoformat() or int(s.get("turns", 0)) >= MAX_TURNS:
        return None
    return s.get("id") or None


def _remember_session(store: Store, target: str, sid: str, now: int) -> None:
    key = session_key(target)
    try:
        prev = json.loads(store.meta_get(key) or "{}")
    except json.JSONDecodeError:
        prev = {}
    turns = int(prev.get("turns", 0)) + 1 if prev.get("id") == sid else 1
    store.meta_set(key, json.dumps({"id": sid, "day": to_local(now).date().isoformat(), "turns": turns}))


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


def _reap(store: Store, ts: int) -> str | None:
    """End every launch that is over: kill the alive ones past the timeout, deliver, remember the session."""
    outcome = None
    for cur in running(store):
        alive = procs.alive(cur.pid) if cur.pid else False
        if alive and ts - cur.ts < TIMEOUT_MS:
            continue
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
            _remember_session(store, cur.project, sid, ts)
        _log.info("launched Claude finished in %s: %s", cur.project or "(owner)", status)
        outcome = "killed" if status == "killed" else "finished"
    return outcome


NODIR_PREFIX = "tg_nodir:"  # last report of a group with no directory (survives a re-exec)


def _nodir_last(store: Store, target: str) -> int:
    try:
        return int(store.meta_get(f"{NODIR_PREFIX}{target}") or 0)
    except (ValueError, TypeError):
        return 0


def _no_directory(target: str, ts: int, reported: dict[str, int]) -> bool:
    """A group with no directory: log it once in a while (not on every tick). True — tell the owner.

    target='' is the hub-wide group: with no project configured there is nowhere to run it, and that is said
    too — an owner launch must never sit idle unnoticed. `reported` (project → when it was last said) is
    the caller's memory — the bot process that runs for weeks, not a module global.
    """
    if ts - reported.get(target, 0) < NO_DIR_MS:
        return False
    reported[target] = ts
    if target:
        _log.warning("cannot launch Claude: no directory for project %s — no such project in the hub config", target)
    else:
        _log.warning("cannot launch Claude: no directory for the hub-wide group — no projects in the hub config")
    return True


def _no_directory_store(store: Store, target: str, ts: int) -> bool:
    """Store-backed `_no_directory`: the dedupe lives in meta, so a re-exec does not re-announce."""
    if ts - _nodir_last(store, target) < NO_DIR_MS:
        return False
    store.meta_set(f"{NODIR_PREFIX}{target}", str(ts))
    if target:
        _log.warning("cannot launch Claude: no directory for project %s — no such project in the hub config", target)
    else:
        _log.warning("cannot launch Claude: no directory for the hub-wide group — no projects in the hub config")
    return True


def tick(store: Store, *, projects: list[config.ProjectConfig] | None = None, now: int | None = None,
         spawn=None, binary: str | None = None, no_dir: dict[str, int] | None = None) -> str:
    """One supervise/launch step.

    Returns what happened: idle | running | finished | killed | launched | limit | nodir:<project>
    (nothing will ever be launched for that project — each group with no directory is said once in NO_DIR_MS,
    one per tick; an empty name — the hub-wide group with no configured project). `no_dir` — the caller's
    memory of what was already reported; None — the store (meta, survives a re-exec).
    """
    ts = now if now is not None else now_ms()
    reported = no_dir
    ended = _reap(store, ts)
    if ended is not None:
        return ended
    alive = {cur.project: cur for cur in running(store)}
    if projects is None:
        projects, _ = config.load_projects()
    targets = _groups(store)
    nodir: list[str] = []  # '' is a target too — the hub-wide group
    for target, msgs in targets:
        if target in alive:
            continue  # the Claude of this project is already on it
        if events.present(store, project=target or None, now=ts):
            continue  # a live session reads its own messages with `ahub inbox`
        if _launches_last_hour(store, ts) >= MAX_PER_HOUR:
            return "limit"
        root = _root_for(target, projects, store)
        exe = binary or claude_bin()
        if exe is None:
            _log.error("cannot launch Claude: no claude binary")
            return "idle"
        if root is None:
            nodir.append(target)
            continue
        scope = Scope((target,)) if target else Scope()
        with ui.plain():  # a prompt for Claude, not a screen — no colour, no marks
            status = views.status_text(store, scope=scope, now=ts)
        prompt = PROMPT.format(project_line=project_line(target),
                               messages="\n".join(f"- {m['text']}" for m in msgs)[:6000],
                               status=status,
                               lang_line=_owner_lang_line())
        resume = _session(store, ts, target)
        with store.tx() as c:
            lid = int(c.execute("INSERT INTO claude_launch(ts, project, session_id, reason) VALUES(?,?,?,?)",
                                (ts, target, resume or "", f"{len(msgs)} messages")).lastrowid)
        log_path = paths.state_dir() / "claude" / f"launch_{lid}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = build_command(exe, prompt, resume)
        if spawn is None:
            with open(log_path, "ab") as out:
                p = subprocess.Popen(cmd, cwd=root, stdout=out, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, start_new_session=True)
            pid = p.pid
        else:
            pid = spawn(cmd, root, str(log_path))
        with store.tx() as c:
            c.execute("UPDATE claude_launch SET pid=? WHERE id=?", (pid, lid))
        # messages count as delivered once the launched Claude has lived a while (see MIN_ALIVE_MS) — not at once
        store.meta_set(f"launch_msgs:{lid}", json.dumps([m["id"] for m in msgs]))
        _log.info("launched Claude in %s (pid %s, %s)", target or "the hub", pid,
                  "resume" if resume else "new session")
        return "launched"
    for target in nodir:
        if reported is not None:
            if _no_directory(target, ts, reported):
                return f"nodir:{target}"  # one per tick; the rest of them are due on the next ones
        elif _no_directory_store(store, target, ts):
            return f"nodir:{target}"  # one per tick; the rest of them are due on the next ones
    return "running" if alive else "idle"
