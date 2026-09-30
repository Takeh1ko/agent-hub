"""Запуск Claude Code из TG, когда живой сессии нет (V27, architecture §11; решение владельца).

- Повод: есть непрочитанные сообщения человека, Claude не отмечается присутствием (wait/watch), запущенного нет.
- Один Claude за раз: пока запущенный работает, новые сообщения копятся — он сам заберёт их `ahub inbox`
  перед завершением, остальное уйдёт следующим запуском.
- Проект: названный в сообщении, иначе тот, где Claude работал последним (presence), иначе первый проект хаба.
- Одна продолжаемая «TG-сессия» (claude --resume) в пределах суток и пока ходов меньше MAX_TURNS.
- `--dangerously-skip-permissions` — как у владельца. Надзор: таймаут, журнал запусков (claude_launch), лимит в час.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ahub import comms, config, events, paths, procs, views
from ahub import log as hublog
from ahub.store import Store
from ahub.time import now_ms, to_local

SESSION_KEY = "tg_claude_session"
TIMEOUT_MS = 30 * 60_000
MAX_TURNS = 30
MAX_PER_HOUR = 6
MIN_ALIVE_MS = 20_000  # прожил меньше и без сессии — сообщения не считаются переданными (перезапуск их заберёт)
_log = hublog.get("launcher")

PROMPT = """Ты — Claude, ведущий разработку через agent-hub (команда `ahub`). Владелец сейчас не за компьютером и
пишет тебе из Telegram, как сеньору в офисе. Твоей живой сессии не было — тебя поднял хаб.

Сообщения владельца:
{messages}

Сводка хаба (ahub status):
{status}

Как работать:
- Отвечай владельцу только через `ahub say "текст"` (коротко, по-русски, без разметки). Вопрос с вариантами —
  `ahub ask "вопрос" --options "да,нет"`; ответ придёт событием (проверь `ahub wait --timeout 10m`).
- Задачи ставь и веди через `ahub` (`ahub --help`, `ahub task new --help`); подробности задачи — `ahub status T<id>`,
  результат — `ahub result T<id>`. Сам код не пиши, если это не мелкая правка поверх результата задачи.
- Сливать код — только с согласия владельца (спроси через `ahub ask`).
- Перед тем как закончить, проверь `ahub inbox` — могли прийти новые сообщения.
"""


def claude_bin() -> str | None:
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
    """Сообщения переданы Claude: прочитаны + их события подтверждены."""
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
    """Один шаг надзора/запуска. Возвращает, что сделали: idle | running | finished | killed | launched | limit."""
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
        _log.info("запущенный Claude завершён: %s", status)
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
        _log.error("не могу поднять Claude: %s", "нет проекта" if project is None else "нет бинаря claude")
        return "idle"
    prompt = PROMPT.format(messages="\n".join(f"- {m['text']}" for m in msgs)[:6000],
                           status=views.status_text(store, now=ts))
    resume = _session(store, ts)
    with store.tx() as c:
        lid = int(c.execute("INSERT INTO claude_launch(ts, project, session_id, reason) VALUES(?,?,?,?)",
                            (ts, project.name, resume or "", f"{len(msgs)} сообщений")).lastrowid)
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
    # сообщения помечаются переданными, когда запущенный Claude поживёт (см. MIN_ALIVE_MS) — не сразу
    store.meta_set(f"launch_msgs:{lid}", json.dumps([m["id"] for m in msgs]))
    _log.info("поднял Claude в %s (pid %s, %s)", project.name, pid, "продолжение" if resume else "новая сессия")
    return "launched"
