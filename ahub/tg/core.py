"""TG bot v2 — pure logic, no network (architecture §11). Network and aiogram live in ahub.tg.run.

TG is not a hub console: it links the human to Claude and shows tasks.
- Any human text → message for Claude (owner_message event); no Claude around — launcher starts one.
- A message belongs to a project: the prefix in the text («по agent-hub: …», "agent-hub: …"), else the project
  this chat picked last (/project, or the prefix of the previous message), else the hub (project='' — its
  message is in every project's inbox; the launcher starts it for the owner only). A pick of a project that is
  no longer in the hub is dropped with a notice, so the message is not stranded.
- Claude → human: `ahub say` (outbox), button questions (`ahub ask`), a button press answers Claude.
- View: /tasks — active and recent with buttons, tap for a plain-words detail. Read-only.
- The hub writes to the human directly only for observer alarms (comms.alarms_for_tg).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ahub import archive, comms, config, events, ui, views
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION
from ahub.store import Store
from ahub.time import fmt_local, now_ms

MSG_LIMIT = 4000
RECENT = 8
PROJECT_KEY = "tg_project"  # the project the owner picked per chat (`tg_project:<chat_id>`) — until the next pick
CLEAR_WORDS = ("all", "hub", "*", "none")  # /project all — back to the hub, no project picked


@dataclass(frozen=True)
class Button:
    label: str
    data: str  # callback_data, at most 64 bytes


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] | None = None


def remember_chat(store: Store, chat_id: int, *, now: int | None = None) -> None:
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        c.execute("INSERT INTO tg_chat(chat_id, first_ts, last_ts) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE"
                  " SET last_ts=excluded.last_ts, dead=0", (chat_id, ts, ts))


def chats(store: Store, hub: config.HubConfig | None = None) -> list[int]:
    """Broadcast chats: live ones from db, else fallback chat_id from config, else []."""
    if hub is None:
        try:
            hub = config.load_hub()
        except config.ConfigError:
            hub = None
    with store.read() as c:
        ids = [r[0] for r in c.execute("SELECT chat_id FROM tg_chat WHERE dead=0 ORDER BY first_ts")]
    if ids:
        return ids
    if hub is not None and hub.tg_chat_id is not None:
        return [hub.tg_chat_id]
    return []


def mark_dead(store: Store, chat_id: int) -> None:
    with store.tx() as c:
        c.execute("UPDATE tg_chat SET dead=1 WHERE chat_id=?", (chat_id,))


def clip(text: str, limit: int = MSG_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 2] + "…"


def split_project(text: str, projects: list[str]) -> tuple[str | None, str]:
    """"po <project>:" (Russian "po" = "for") / "<project>:" → (project, text). Else (None, text)."""
    s = text.strip()
    low = s.lower()
    for name in sorted(projects, key=len, reverse=True):
        for prefix in (f"по {name.lower()}:", f"{name.lower()}:", f"for {name.lower()}:"):
            if low.startswith(prefix):
                return name, s[len(prefix):].strip()
    return None, s


def current_project(store: Store, chat_id: int) -> str:
    """The project the owner picked in that chat; '' — none (a message then goes to the hub)."""
    return store.meta_get(f"{PROJECT_KEY}:{chat_id}") or ""


def pick_project(store: Store, chat_id: int, want: str, projects: list[str]) -> str | None:
    """Remember the project of the next messages of that chat (`/project X` or a prefix). None — no such project."""
    name = next((p for p in projects if p.lower() == want.strip().lower()), None)
    if name is not None:
        store.meta_set(f"{PROJECT_KEY}:{chat_id}", name)
    return name


def forget_project(store: Store, chat_id: int) -> None:
    """No project picked in that chat — a plain message goes to the hub again (the owner's session)."""
    store.meta_del(f"{PROJECT_KEY}:{chat_id}")


def project_reply(store: Store, projects: list[str], want: str | None = None, chat_id: int = 0) -> Reply:
    """`/project X` — switch to that project, `/project all|hub|*|none` — back to the hub;
    `/project` (or a button) — the current one and the list. The pick is per chat."""
    if want:
        if want.strip().lower() in CLEAR_WORDS:
            forget_project(store, chat_id)
            return Reply(_t("tg.project_none"))
        name = pick_project(store, chat_id, want, projects)
        if name:
            return Reply(_t("tg.project_set", name=name))
        if projects:
            return Reply(_t("tg.project_unknown", name=want.strip(), projects=", ".join(projects)))
        return Reply(_t("tg.project_unknown_none", name=want.strip()))
    cur = current_project(store, chat_id)
    head = _t("tg.project_now", name=cur) if cur in projects else _t("tg.project_none")
    rows = [[Button(_t("tg.project_mark", name=p) if p == cur else p, f"proj:{p}")] for p in projects]
    if cur:
        rows.append([Button(_t("tg.project_tag_any"), f"proj:{CLEAR_WORDS[0]}")])
    return Reply(head, rows or None)


def _project_tag(project: str) -> str:
    return _t("tg.project_tag", name=project) if project else _t("tg.project_tag_any")


def on_text(store: Store, chat_id: int, text: str, *, projects: list[str], now: int | None = None) -> Reply:
    """Human free text → message for Claude, in the project of the prefix, the last pick of this chat, or the hub.

    A pick that is no longer in the hub is dropped, and the notice says where the message actually went: to
    the hub — the launcher starts that for the owner, and project='' rows are in every project's inbox — or
    to the project the prefix named.
    """
    remember_chat(store, chat_id, now=now)
    picked, body = split_project(text, projects)
    if not body:
        return Reply(_t("tg.empty"))
    cur = current_project(store, chat_id)
    stale = cur and cur not in projects
    if stale:
        forget_project(store, chat_id)
    if picked:
        pick_project(store, chat_id, picked, projects)
    project = picked or ("" if stale else cur)
    comms.owner_message(store, body, project=project, chat_id=chat_id, now=now)
    online = events.present(store, project=project or None, now=now)
    line = _t("tg.sent") if online else _t("tg.launching")
    # the notice must say where the message actually went — a prefix may have named a project
    notice = ""
    if stale:
        notice = (_t("tg.project_gone_to", name=cur, project=project) if project
                  else _t("tg.project_gone", name=cur)) + " "
    return Reply(f"{notice}{line} · {_project_tag(project)}")


def help_text() -> str:
    return _t("tg.help")


def status_text(store: Store) -> str:
    """Telegram has no terminal: the hub text stays plain (ui.plain), never coloured for nobody."""
    with ui.plain():
        return views.status_text(store)


def _task_label(t) -> str:
    st = archive.STATE_WORDS.get(t.state.value, t.state.value)
    return f"{t.label} · {st} · {ui.clip(t.title, 30)}"[:60]


def tasks_reply(store: Store) -> Reply:
    active = store.list_tasks(states=ACTIVE | WAITING_DECISION)
    recent = [t for t in store.list_tasks(newest_first=True, limit=RECENT * 3)
              if t not in active and t.state.value in ("accepted", "rejected")][:RECENT]
    rows = [[Button(_task_label(t), f"task:{t.id}")] for t in active + recent]
    if not rows:
        return Reply(_t("tg.no_tasks"))
    head = _t("tg.tasks_head", active=len(active), recent=len(recent))
    return Reply(head, rows)


def task_detail(store: Store, task_id: int) -> Reply:
    t = store.get_task(task_id)
    if t is None:
        return Reply(_t("tg.no_task", tid=task_id))
    from ahub import pulse
    from ahub.service import live_workers

    live = live_workers()
    with ui.plain():
        text = views.task_text(store, t, live=live)
    if t.state in ACTIVE:
        pl = pulse.task_pulse(store, t, live=live)
        text = f"{pl.mark} {pl.reason or _t('tui.working_now')}\n" + text
    text += "\n" + _t("tg.created", when=fmt_local(t.created_at))
    return Reply(clip(text), [[Button(_t("tg.to_list"), "tasks")]])


def question_reply(q: dict) -> Reply:
    opts = q.get("options") or json.loads(q.get("options_json") or "[]")
    rows = [[Button(o[:40], f"ans:{q['id']}:{i}")] for i, o in enumerate(opts)]
    return Reply(_t("tg.question", qid=q["id"], text=q["text"]), rows or None)


def on_answer_button(store: Store, data: str, *, via: str = "tg") -> str:
    """`ans:<qid>:<i>` → question answer. Returns text for editing the message."""
    try:
        _, qid, idx = data.split(":")
        qid_i, idx_i = int(qid), int(idx)
    except ValueError:
        return _t("tg.btn_unknown")
    with store.read() as c:
        row = c.execute("SELECT * FROM question WHERE id=?", (qid_i,)).fetchone()
    if row is None:
        return _t("tg.q_missing")
    opts = json.loads(row["options_json"] or "[]")
    if not 0 <= idx_i < len(opts):
        return _t("tg.q_bad_option")
    if not comms.answer(store, qid_i, opts[idx_i], via=via):
        return _t("tg.q_answered", qid=qid_i, answer=row["answer"])
    return _t("tg.q_sent", qid=qid_i, text=row["text"], opt=opts[idx_i])


def on_reply_to_question(store: Store, qid: int, text: str) -> str:
    if comms.answer(store, qid, text, via="tg"):
        return _t("tg.reply_sent", qid=qid)
    return _t("tg.reply_closed", qid=qid)


def pending_questions(store: Store) -> list[dict]:
    with store.read() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM question WHERE status='open' AND tg_sent_at IS NULL")]
    return rows


def mark_question_sent(store: Store, qid: int) -> None:
    with store.tx() as c:
        c.execute("UPDATE question SET tg_sent_at=? WHERE id=?", (now_ms(), qid))


def alarm_text(e) -> str:
    return ("🚨 " if e.critical else "⚠️ ") + _t("tg.alarm_prefix") + str(e.payload.get("text", ""))[:1000]
