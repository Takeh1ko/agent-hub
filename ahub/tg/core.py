"""TG-бот v2 — чистая логика без сети (architecture §11). Сеть и aiogram — в ahub.tg.run.

TG — не пульт хаба: связь человека с Claude и просмотр задач.
- Любой текст человека → сообщение для Claude (событие owner_message); если Claude нет — его поднимет launcher.
- Claude → человек: `ahub say` (outbox), вопросы с кнопками (`ahub ask`), кнопка = ответ Claude.
- Просмотр: /tasks — активные и недавние кнопками, нажал — подробно словами. Только чтение.
- Напрямую хаб пишет человеку только тревоги наблюдателя (comms.alarms_for_tg).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ahub import archive, comms, events, views
from ahub.model import ACTIVE, WAITING_DECISION
from ahub.store import Store
from ahub.time import fmt_local, now_ms

MSG_LIMIT = 4000
RECENT = 8


@dataclass(frozen=True)
class Button:
    label: str
    data: str  # callback_data ≤ 64 байт


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] | None = None


def remember_chat(store: Store, chat_id: int, *, now: int | None = None) -> None:
    ts = now if now is not None else now_ms()
    with store.tx() as c:
        c.execute("INSERT INTO tg_chat(chat_id, first_ts, last_ts) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE"
                  " SET last_ts=excluded.last_ts, dead=0", (chat_id, ts, ts))


def chats(store: Store) -> list[int]:
    from ahub.secrets import OWNER_CHAT_ID

    with store.read() as c:
        ids = [r[0] for r in c.execute("SELECT chat_id FROM tg_chat WHERE dead=0 ORDER BY first_ts")]
    return ids or [OWNER_CHAT_ID]


def mark_dead(store: Store, chat_id: int) -> None:
    with store.tx() as c:
        c.execute("UPDATE tg_chat SET dead=1 WHERE chat_id=?", (chat_id,))


def clip(text: str, limit: int = MSG_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 2] + "…"


def split_project(text: str, projects: list[str]) -> tuple[str | None, str]:
    """«по agent-hub: …» / «agent-hub: …» → (проект, текст). Иначе (None, текст)."""
    s = text.strip()
    low = s.lower()
    for name in sorted(projects, key=len, reverse=True):
        for prefix in (f"по {name.lower()}:", f"{name.lower()}:"):
            if low.startswith(prefix):
                return name, s[len(prefix):].strip()
    return None, s


def on_text(store: Store, chat_id: int, text: str, *, projects: list[str], now: int | None = None) -> Reply:
    """Свободный текст человека → сообщение для Claude."""
    remember_chat(store, chat_id, now=now)
    project, body = split_project(text, projects)
    if not body:
        return Reply("пустое сообщение — не отправлено")
    comms.owner_message(store, body, project=project or "", chat_id=chat_id, now=now)
    if events.present(store, now=now):
        return Reply("передал Claude — он на связи")
    return Reply("Claude сейчас не в сессии — поднимаю его, ответ придёт сюда")


def help_text() -> str:
    return ("Я связываю тебя с Claude, который ведёт задачи в agent-hub.\n"
            "• Пиши обычным текстом — сообщение уйдёт Claude (если его нет в сессии, я его подниму).\n"
            "• «по agent-hub: …» — если речь о конкретном проекте.\n"
            "• /tasks — задачи: активные и недавние, нажми на задачу — подробности.\n"
            "• /status — коротко, что происходит.\n"
            "Управлять задачами отсюда нельзя — только через Claude.")


def status_text(store: Store) -> str:
    return views.status_text(store)


def _task_label(t) -> str:
    st = archive.STATE_WORDS.get(t.state.value, t.state.value)
    return f"{t.label} · {st} · {views._short(t.title, 30)}"[:60]


def tasks_reply(store: Store) -> Reply:
    active = store.list_tasks(states=ACTIVE | WAITING_DECISION)
    recent = [t for t in store.list_tasks(newest_first=True, limit=RECENT * 3)
              if t not in active and t.state.value in ("accepted", "rejected")][:RECENT]
    rows = [[Button(_task_label(t), f"task:{t.id}")] for t in active + recent]
    if not rows:
        return Reply("задач нет")
    head = f"в работе и ждут решения: {len(active)}; недавние: {len(recent)}"
    return Reply(head, rows)


def task_detail(store: Store, task_id: int) -> Reply:
    t = store.get_task(task_id)
    if t is None:
        return Reply(f"нет задачи T{task_id}")
    from ahub import pulse
    from ahub.service import live_workers

    live = live_workers()
    text = views.task_text(store, t, live=live)
    if t.state in ACTIVE:
        pl = pulse.task_pulse(store, t, live=live)
        text = f"{pl.mark} {pl.reason or 'работает'}\n" + text
    text += f"\nсоздана {fmt_local(t.created_at)}"
    return Reply(clip(text), [[Button("← к списку", "tasks")]])


def question_reply(q: dict) -> Reply:
    opts = q.get("options") or json.loads(q.get("options_json") or "[]")
    rows = [[Button(o[:40], f"ans:{q['id']}:{i}")] for i, o in enumerate(opts)]
    return Reply(f"❓ Вопрос от Claude (#{q['id']}):\n{q['text']}\n\n(можно ответить текстом — реплаем на это сообщение)",
                 rows or None)


def on_answer_button(store: Store, data: str, *, via: str = "tg") -> str:
    """«ans:<qid>:<i>» → ответ на вопрос. Возвращает текст для правки сообщения."""
    try:
        _, qid, idx = data.split(":")
        qid_i, idx_i = int(qid), int(idx)
    except ValueError:
        return "не понял кнопку"
    with store.read() as c:
        row = c.execute("SELECT * FROM question WHERE id=?", (qid_i,)).fetchone()
    if row is None:
        return "вопрос не найден"
    opts = json.loads(row["options_json"] or "[]")
    if not 0 <= idx_i < len(opts):
        return "нет такого варианта"
    if not comms.answer(store, qid_i, opts[idx_i], via=via):
        return f"#{qid_i}: уже отвечено ({row['answer']})"
    return f"#{qid_i}: {row['text']}\n→ {opts[idx_i]} (передал Claude)"


def on_reply_to_question(store: Store, qid: int, text: str) -> str:
    if comms.answer(store, qid, text, via="tg"):
        return f"ответ на #{qid} передал Claude"
    return f"#{qid}: уже отвечено или закрыт"


def pending_questions(store: Store) -> list[dict]:
    with store.read() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM question WHERE status='open' AND tg_sent_at IS NULL")]
    return rows


def mark_question_sent(store: Store, qid: int) -> None:
    with store.tx() as c:
        c.execute("UPDATE question SET tg_sent_at=? WHERE id=?", (now_ms(), qid))


def alarm_text(e) -> str:
    return ("🚨 " if e.critical else "⚠️ ") + "Хаб: " + str(e.payload.get("text", ""))[:1000]
