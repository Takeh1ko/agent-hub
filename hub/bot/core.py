"""Логика TG-пульта без сети: функции от store/snapshot → текст+кнопки.

Тестируется без Telegram. Время — параметром now_ms, чтобы тесты шли
на фиксированных часах. Короткие соединения sqlite, закрываются сразу.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hub.store import Store

# Пороги из карточки H05.
LISTEN_MS = 10 * 60_000
GROUP_MS = 5 * 60_000
MSG_LIMIT = 4000
TASK_LIMIT = 3500
GO_LIMIT = 60.0
# Сводки: какие kinds группируются в одно сообщение.
NOTIFY_KINDS = frozenset({
    "ready", "arbiter", "failed", "stuck", "crashed",
    "budget_soft", "budget_hard",
})

START_TEXT = "Привет! Я пульт agent-hub.\nСвободный текст — Claude, /help — команды."
HELP_TEXT = (
    "Команды:\n"
    "/status — картина\n"
    "/roster — команда\n"
    "/task ID — задача\n"
    "/stop ID, /merge ID — с подтверждением\n"
    "/budget — лимиты\n"
    "/pause, /resume — очередь\n"
    "Свободный текст — сообщение Claude."
)


@dataclass
class Button:
    """Инлайн-кнопка: подпись и callback_data."""

    label: str
    data: str


@dataclass
class Grouper:
    """Группировка событий за окно в одно сообщение.

    seen — id уже взятых событий: повторный забор тех же строк
    (sent_tg ставится только после отправки) не дублирует буфер.
    """

    window_ms: int = GROUP_MS
    buf: list[dict] = field(default_factory=list)
    first_ts: int | None = None
    seen: set[int] = field(default_factory=set)

    def add(self, event: dict, now_ms: int) -> bool:
        """Положить событие в буфер. Дубль по id — пропустить (False)."""
        eid = event.get("id") if isinstance(event, dict) else None
        try:
            key = int(eid) if eid is not None else None
        except (TypeError, ValueError):
            key = None
        if key is not None:
            if key in self.seen:
                return False
            self.seen.add(key)
        if self.first_ts is None:
            self.first_ts = int(now_ms)
        self.buf.append(dict(event))
        return True

    def ready(self, now_ms: int) -> bool:
        """Окно вышло и есть что слать."""
        if not self.buf or self.first_ts is None:
            return False
        return int(now_ms) - int(self.first_ts) >= self.window_ms

    def peek(self) -> str:
        """Слить буфер в сообщение без очистки (отправка может упасть)."""
        return format_grouped(self.buf)

    def pending_ids(self) -> list[int]:
        """Id событий в буфере."""
        out: list[int] = []
        for e in self.buf:
            try:
                out.append(int(e.get("id")))
            except (TypeError, ValueError, AttributeError):
                continue
        return out

    def clear(self) -> None:
        """Очистить буфер (только после успешной отправки)."""
        self.buf.clear()
        self.first_ts = None

    def drop(self, ids: list[int]) -> None:
        """Убрать из seen (после mark_events_sent — рост не бесконечный)."""
        for i in ids:
            try:
                self.seen.discard(int(i))
            except (TypeError, ValueError):
                continue

    def flush(self) -> str:
        """Слить буфер в одно сообщение и очистить (id — из seen)."""
        ids = self.pending_ids()
        text = format_grouped(self.buf)
        self.clear()
        self.drop(ids)
        return text

    def remove_ids(self, ids: list[int]) -> None:
        """Убрать из буфера только события с id (частичная отправка).

        Остаток ждёт следующее окно: first_ts перезапускается вызывающим,
        seen чистится только по ушедшим id.
        """
        want = set()
        for i in ids:
            try:
                want.add(int(i))
            except (TypeError, ValueError):
                continue
        kept = []
        for e in self.buf:
            try:
                key = int(e.get("id"))
            except (TypeError, ValueError, AttributeError):
                kept.append(e)
                continue
            if key not in want:
                kept.append(e)
        self.buf = kept
        self.drop(list(want))
        if not self.buf:
            self.first_ts = None


def esc(text: str) -> str:
    """Экранировать для parse_mode HTML."""
    return html.escape(str(text), quote=False)


def clip(text: str, limit: int = MSG_LIMIT) -> str:
    """Обрезать до limit символов."""
    s = str(text)
    if len(s) <= limit:
        return s
    return s[:limit]


def _wrap_pre(body: str, limit: int) -> str:
    """Обернуть в <pre>, длина считается после esc, </pre> цел."""
    inner = esc(clip(str(body), max(0, limit - 11)))
    if len(inner) > limit - 11:
        inner = inner[:limit - 11]
        inner = re.sub(r"&[A-Za-z#0-9]*$", "", inner)
    return f"<pre>{inner}</pre>"


_QREF = re.compile(r"Вопрос\s*#(\d+)")


def parse_question_ref(text: str) -> int | None:
    """Достать id вопроса из текста («❓ Вопрос #12 …») — fallback реплая."""
    m = _QREF.search(str(text or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _con(store: Store) -> sqlite3.Connection:
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    return con


def meta_get(store: Store, key: str) -> str | None:
    """Прочитать meta.key, None если нет."""
    con = _con(store)
    try:
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row is not None else None
    finally:
        con.close()


def meta_set(store: Store, key: str, value: str) -> None:
    """Записать meta.key."""
    con = _con(store)
    try:
        con.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    finally:
        con.close()


def remember_chat(store: Store, chat_id: int, now_ms: int) -> None:
    """Запомнить чат владельца для рассылки."""
    con = _con(store)
    try:
        con.execute(
            "INSERT INTO tg_chat(chat_id, first_ts) VALUES (?, ?)"
            " ON CONFLICT(chat_id) DO NOTHING",
            (int(chat_id), int(now_ms)),
        )
        con.commit()
    finally:
        con.close()
    # Чат снова написал — снимаем признак мёртвого (включая владельца).
    try:
        clear_chat_dead(store, int(chat_id))
    except Exception:
        pass


def list_chats(store: Store) -> list[int]:
    """Чаты из tg_chat по порядку first_ts."""
    con = _con(store)
    try:
        rows = con.execute(
            "SELECT chat_id FROM tg_chat ORDER BY first_ts, chat_id").fetchall()
        return [int(r["chat_id"]) for r in rows]
    finally:
        con.close()


def forget_chat(store: Store, chat_id: int) -> None:
    """Удалить мёртвый чат из рассылки (Telegram: chat not found/blocked).

    Владельца (OWNER_CHAT_ID нет в tg_chat) помечаем мёртвым отдельно,
    чтобы all_chats() его исключал до следующего сообщения.
    """
    con = _con(store)
    try:
        con.execute("DELETE FROM tg_chat WHERE chat_id=?", (int(chat_id),))
        con.commit()
    finally:
        con.close()
    try:
        mark_chat_dead(store, int(chat_id))
    except Exception:
        pass


# Подстроки мёртвого чата (Telegram): такой чат удаляем, а не ретраим.
_DEAD_HINTS = (
    "chat not found",
    "chat_not_found",
    "bot was blocked",
    "bot_was_blocked",
    "bot blocked",
    "forbidden",
    "was deleted",
    "chat was deleted",
    "group chat was deleted",
    "user is deactivated",
    "user was deleted",
    "user not found",
    "user_not_found",
    "peer_id_invalid",
    "peer id invalid",
    "chat_id is empty",
    "chat id is empty",
    "chat_id_is_empty",
)


def _dead_set(store: Store) -> set[int]:
    """Мёртвые чаты из meta.tg_dead_chats (JSON-список)."""
    raw = meta_get(store, "tg_dead_chats")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    out: set[int] = set()
    for x in data:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def mark_chat_dead(store: Store, chat_id: int) -> None:
    """Запомнить мёртвый чат (включая владельца) — исключается из рассылки."""
    dead = _dead_set(store) | {int(chat_id)}
    meta_set(store, "tg_dead_chats", json.dumps(sorted(dead)[-1000:]))


def clear_chat_dead(store: Store, chat_id: int) -> None:
    """Чат ожил (написал боту) — убрать из мёртвых."""
    dead = _dead_set(store)
    if int(chat_id) not in dead:
        return
    dead.discard(int(chat_id))
    meta_set(store, "tg_dead_chats", json.dumps(sorted(dead)[-1000:]))


def is_dead_chat_error(exc: BaseException | object) -> bool:
    """Мёртвый ли чат по исключению отправки (удалить, не держать очередь).

    Telegram отвечает «chat not found» / «bot was blocked» / «Forbidden»:
    такой чат уже не оживёт — ретраить каждые 5 с бесконечно нельзя.
    """
    try:
        name = type(exc).__name__.lower()
    except Exception:
        name = ""
    if "forbidden" in name:
        return True
    try:
        text = str(exc or "").lower()
    except Exception:
        return False
    return any(h in text for h in _DEAD_HINTS)


def all_chats(store: Store) -> list[int]:
    """Все получатели рассылки: tg_chat + OWNER_CHAT_ID без мёртвых.

    Мёртвый владелец исключается, пока снова не напишет боту
    (remember_chat снимает признак).
    """
    from hub.tg_send import OWNER_CHAT_ID

    try:
        dead = _dead_set(store)
    except Exception:
        dead = set()
    seen: list[int] = []
    for cid in list_chats(store) + [int(OWNER_CHAT_ID)]:
        if int(cid) in dead:
            continue
        if cid not in seen:
            seen.append(cid)
    return seen


def get_listen_ts(store: Store) -> int | None:
    """Когда Claude последний раз делал hub wait (meta.claude_listen_ts)."""
    raw = meta_get(store, "claude_listen_ts")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _add_event(store: Store, task_id: str, kind: str,
               payload: dict | None, now_ms: int) -> int:
    con = _con(store)
    try:
        cur = con.execute(
            "INSERT INTO event(ts, task_id, kind, payload_json, seen_claude, sent_tg)"
            " VALUES (?, ?, ?, ?, 0, 0)",
            (int(now_ms), str(task_id or ""), str(kind),
             json.dumps(payload or {}, ensure_ascii=False)),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def handle_owner_text(store: Store, chat_id: int, text: str, now_ms: int) -> str:
    """Свободный текст → inbox(source='tg') + событие owner_message.

    Возвращает ответ бота по свежести meta.claude_listen_ts.
    """
    body = str(text or "").strip()
    remember_chat(store, int(chat_id), int(now_ms))
    if not body:
        return "Пустое сообщение — нечего передавать."
    con = _con(store)
    try:
        con.execute(
            "INSERT INTO inbox(ts, text, source, seen_claude) VALUES (?, ?, 'tg', 0)",
            (int(now_ms), body),
        )
        con.commit()
    finally:
        con.close()
    _add_event(store, "", "owner_message",
               {"text": body[:500], "chat_id": int(chat_id)}, int(now_ms))
    listen = get_listen_ts(store)
    if listen is not None and int(now_ms) - listen < LISTEN_MS:
        return "✅ Передал Claude"
    if listen is not None:
        mins = max(1, (int(now_ms) - listen) // 60_000)
        return (
            f"📥 Claude сейчас не слушает (был {mins} мин назад)"
            " — сообщение в очереди"
        )
    return "📥 Claude сейчас не слушает — сообщение в очереди"


def fetch_outbox(store: Store) -> list[dict]:
    """Неотправленные строки outbox (sent_ts IS NULL)."""
    con = _con(store)
    try:
        rows = con.execute(
            "SELECT * FROM outbox WHERE sent_ts IS NULL ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def mark_outbox_sent(store: Store, outbox_id: int, now_ms: int) -> None:
    """Отметить строку outbox отправленной."""
    con = _con(store)
    try:
        con.execute("UPDATE outbox SET sent_ts=? WHERE id=?",
                    (int(now_ms), int(outbox_id)))
        con.commit()
    finally:
        con.close()


def parse_options(row: dict) -> list[str]:
    """Варианты вопроса из options_json, битый JSON → []."""
    raw = row.get("options_json") if isinstance(row, dict) else None
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if str(x).strip()]
    try:
        data = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if str(x).strip()]


def get_question(store: Store, qid: int) -> dict | None:
    """Строка question по id."""
    con = _con(store)
    try:
        row = con.execute("SELECT * FROM question WHERE id=?",
                          (int(qid),)).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def list_open_questions(store: Store) -> list[dict]:
    """Вопросы со status='open' по порядку id."""
    con = _con(store)
    try:
        rows = con.execute(
            "SELECT * FROM question WHERE status='open' ORDER BY id").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# Лимит Telegram на текст инлайн-кнопки.
BUTTON_LABEL_LIMIT = 64


def format_question(row: dict) -> tuple[str, list[Button]]:
    """Текст вопроса + кнопки по options_json.

    Подпись кнопки режется до BUTTON_LABEL_LIMIT: длинный вариант иначе
    роняет send_message (400) во все чаты и вопрос ретраится бесконечно.
    """
    qid = int(row.get("id") or 0)
    task_id = str(row.get("task_id") or "")
    text = str(row.get("text") or "")
    opts = parse_options(row)
    head = f"❓ Вопрос #{qid}"
    if task_id:
        head += f" [{task_id}]"
    body = f"{head}:\n{text}"
    if not opts:
        body += "\nОтветить текстом (реплаем)."
        return body, []
    buttons = [Button(label=o[:BUTTON_LABEL_LIMIT], data=f"qans:{qid}:{i}")
               for i, o in enumerate(opts)]
    return body, buttons


def format_answered_text(row: dict, answer: str) -> str:
    """Исходный вопрос + дописка «Ответ: …», кнопки убраны."""
    base, _ = format_question(row)
    return f"{base}\nОтвет: {answer}"


ANSWER_OK = "ok"
ANSWER_NOT_FOUND = "not_found"
ANSWER_EMPTY = "empty"
ANSWER_ALREADY = "already"


def answer_question_reason(store: Store, qid: int, answer: str,
                           via: str = "tg", now_ms: int = 0) -> str:
    """Ответить на вопрос, вернув причину исхода.

    ok — записан (событие answer); not_found — нет такого вопроса;
    empty — пустой текст; already — уже отвечен (без дубля события).
    Атомарно: UPDATE с условием status<>'answered', событие только
    при rowcount==1.
    """
    body = str(answer or "").strip()
    if not body:
        return ANSWER_EMPTY
    con = _con(store)
    try:
        row = con.execute("SELECT task_id FROM question WHERE id=?",
                          (int(qid),)).fetchone()
        if row is None:
            return ANSWER_NOT_FOUND
        task_id = str(row["task_id"] or "")
        cur = con.execute(
            "UPDATE question SET status='answered', answer=?, answered_via=?"
            " WHERE id=? AND status<>'answered'",
            (body, str(via), int(qid)),
        )
        if cur.rowcount != 1:
            return ANSWER_ALREADY
        con.commit()
    finally:
        con.close()
    _add_event(store, task_id, "answer",
               {"question_id": int(qid), "answer": body[:500], "via": str(via)},
               int(now_ms))
    return ANSWER_OK


def answer_question(store: Store, qid: int, answer: str,
                    via: str = "tg", now_ms: int = 0) -> bool:
    """Ответить на вопрос: status='answered', событие answer.

    Совместимая обёртка: True только при ANSWER_OK.
    Причину неуспеха даёт answer_question_reason.
    """
    return answer_question_reason(store, qid, answer,
                                  via=via, now_ms=now_ms) == ANSWER_OK


def parse_answer_callback(data: str) -> tuple[int, int] | None:
    """Разобрать callback «qans:<qid>:<idx>»."""
    parts = str(data or "").split(":")
    if len(parts) != 3 or parts[0] != "qans":
        return None
    try:
        return int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None


def answer_by_callback(store: Store, data: str,
                       via: str = "tg", now_ms: int = 0) -> tuple[bool, str]:
    """Ответ кнопкой: найти вариант и записать. Возвращает (ок, текст)."""
    parsed = parse_answer_callback(data)
    if parsed is None:
        return False, "Не понял кнопку."
    qid, idx = parsed
    row = get_question(store, qid)
    if row is None:
        return False, "Вопрос не найден."
    opts = parse_options(row)
    if idx < 0 or idx >= len(opts):
        return False, "Вариант устарел."
    ok = answer_question(store, qid, opts[idx], via=via, now_ms=int(now_ms))
    if not ok:
        return False, "Уже отвечен."
    fresh = get_question(store, qid)
    if fresh is None:
        return True, f"Ответ: {opts[idx]}"
    return True, format_answered_text(fresh, opts[idx])


def is_notifiable(event: dict) -> bool:
    """Нужна ли сводка в чат по kind."""
    return str(event.get("kind") or "") in NOTIFY_KINDS


def format_event_line(event: dict) -> str:
    """Одна строка события для сводки."""
    kind = str(event.get("kind") or "?")
    task_id = str(event.get("task_id") or "")
    payload: object = {}
    if isinstance(event.get("payload"), dict):
        # Свежие строки продюсера (record_events) несут payload-дикт.
        payload = event.get("payload")
    else:
        payload_raw = str(event.get("payload_json") or "{}")
        try:
            payload = json.loads(payload_raw)
        except (TypeError, ValueError):
            payload = {}
    extra = ""
    if isinstance(payload, dict):
        for key in ("stage", "text", "answer", "action"):
            if payload.get(key):
                extra = str(payload[key])[:80]
                break
    if task_id and extra:
        return f"{kind} {task_id}: {extra}"
    if task_id:
        return f"{kind} {task_id}"
    if extra:
        return f"{kind}: {extra}"
    return kind


def format_grouped(events: list[dict]) -> str:
    """Несколько событий в одно сообщение (≤ MSG_LIMIT)."""
    lines = [format_event_line(e) for e in events if e]
    if not lines:
        return ""
    head = f"Сводка ({len(lines)}):"
    text = head + "\n" + "\n".join(lines)
    return clip(text, MSG_LIMIT)


def select_summary_batch(events: list[dict],
                         limit: int = MSG_LIMIT) -> tuple[str, list[int]]:
    """Текст сводки из целых строк + id вошедших событий.

    Строки добавляются, пока следующая влезает в limit (заголовок
    «Сводка (N):» считается). Первое событие входит всегда (режется
    clip'ом), чтобы очередь не встала. Остаток — следующим сообщением.
    """
    items = [e for e in events if e]
    if not items:
        return "", []
    lines = [format_event_line(e) for e in items]
    ids: list[int] = []
    for e in items:
        try:
            ids.append(int(e.get("id")))
        except (TypeError, ValueError, AttributeError):
            ids.append(-1)
    # Заголовок зависит от N — подбираем максимальное N целых строк.
    chosen = 0
    for n in range(1, len(lines) + 1):
        head = f"Сводка ({n}):"
        text = head + "\n" + "\n".join(lines[:n])
        if len(text) <= limit:
            chosen = n
        else:
            break
    if chosen == 0:
        # Даже одна строка не влезает — шлём её одну, обрезанную.
        text = clip(f"Сводка (1):\n{lines[0]}", limit)
        return text, [ids[0]] if ids[0] >= 0 else []
    head = f"Сводка ({chosen}):"
    return head + "\n" + "\n".join(lines[:chosen]), \
        [i for i in ids[:chosen] if i >= 0]


def fetch_unsent_notifiable(store: Store) -> list[dict]:
    """События для сводки: sent_tg=0 и kind из NOTIFY_KINDS (фильтр в SQL)."""
    kinds = sorted(NOTIFY_KINDS)
    con = _con(store)
    try:
        rows = con.execute(
            "SELECT * FROM event WHERE sent_tg=0 AND kind IN "
            f"({','.join('?' for _ in kinds)}) ORDER BY id",
            kinds,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def mark_events_sent(store: Store, ids: list[int]) -> None:
    """Отметить события отправленными в TG."""
    ids = [int(i) for i in ids]
    if not ids:
        return
    con = _con(store)
    try:
        con.execute(
            "UPDATE event SET sent_tg=1 WHERE id IN "
            f"({','.join('?' for _ in ids)})",
            ids,
        )
        con.commit()
    finally:
        con.close()


def record_events(store: Store, events: list[dict], now_ms: int) -> list[dict]:
    """Записать события продюсера (snapshot-дифф) в store, вернуть строки с id.

    Возвращаемая строка несёт и payload_json (как таблица event), чтобы
    format_event_line давал те же детали, что и для строк из БД.
    """
    out: list[dict] = []
    for e in events:
        payload = dict(e.get("payload") or {})
        eid = _add_event(store, str(e.get("task_id") or ""),
                         str(e.get("kind") or ""), payload,
                         int(now_ms))
        row = dict(e)
        row["id"] = eid
        row["payload"] = payload
        row["payload_json"] = json.dumps(payload, ensure_ascii=False)
        out.append(row)
    return out


_FINAL_STAGES = ("merged", "dropped")

# Этапы, где пульс ⚫/🔴 без процесса — норма, а не падение:
# задача уже не работает (ждёт merge, в арбитраже, остановлена...)
# либо процесса не должно быть в принципе (очередь, предполёт).
# Отдельно от _FINAL_STAGES: та используется в apply_confirm для /stop.
_QUIET_STAGES = ("queued", "preflight", "ready", "arbiter", "failed",
                 "stopped", "merged", "dropped")


def _pulse_state(t) -> dict | None:
    """Текущее плохое пульс-состояние задачи, None — всё хорошо.

    Тихо для _QUIET_STAGES и для задач без сессий (там пульс считается
    из updated_at и ни о чём не говорит).
    """
    if str(t.stage) in _QUIET_STAGES:
        return None
    if not getattr(t, "sessions", None):
        return None
    if t.pulse == "🔴":
        return {"task_id": t.id, "kind": "stuck",
                "payload": {"stage": str(t.stage)}}
    if t.pulse == "⚫":
        return {"task_id": t.id, "kind": "crashed",
                "payload": {"stage": str(t.stage)}}
    return None


def snapshot_events(prev, cur, pending: dict | None = None) -> list[dict]:
    """Разница снимков → события (чисто, без БД).

    prev None — baseline, событий нет (не спамим при старте).
    Этап → ready/arbiter/failed сразу; пульс → stuck (🔴) / crashed (⚫).
    Для задач в _QUIET_STAGES и без сессий пульс-события не порождаются
    (⚫/🔴 без процесса там — норма, а не падение).

    pending — отложенные пульс-сигналы между тиками (гистерезис):
    None — прежнее поведение (сигнал сразу); dict — сигнал публикуется,
    только если подтверждён следующим снимком, иначе сбрасывается
    (этап стал тихим, пульс вернулся). Живёт в BotState, переживает тики.
    """
    if prev is None or cur is None:
        return []
    old = {t.id: t for t in (getattr(prev, "tasks", None) or [])}
    out: list[dict] = []
    cur_tasks = list(getattr(cur, "tasks", None) or [])
    for t in cur_tasks:
        p = old.get(t.id)
        if p is None:
            continue
        if str(t.stage) != str(p.stage):
            if t.stage == "ready":
                out.append({"task_id": t.id, "kind": "ready",
                            "payload": {"stage": t.stage}})
            elif t.stage == "arbiter":
                out.append({"task_id": t.id, "kind": "arbiter",
                            "payload": {"stage": t.stage}})
            elif t.stage == "failed":
                out.append({"task_id": t.id, "kind": "failed",
                            "payload": {"stage": t.stage}})
        state = _pulse_state(t)
        if pending is None:
            # Прежнее поведение: сигнал только при переходе пульса.
            if state is not None and str(p.pulse) != str(t.pulse):
                out.append(state)
            continue
        if state is None:
            # Сигнал не подтверждён: пульс вернулся или этап стал тихим.
            pending.pop(t.id, None)
            continue
        prev_state = pending.get(t.id)
        if prev_state is not None and prev_state.get("kind") == state["kind"]:
            # Плохое состояние держится второй снимок подряд — публикуем.
            out.append(state)
            pending.pop(t.id, None)
        else:
            pending[t.id] = state
    if pending is not None:
        alive = {t.id for t in cur_tasks}
        for tid in [k for k in pending if k not in alive]:
            pending.pop(tid, None)
    return out


_BUDGET_KEY = "tg_budget_fired"


def _budget_levels(store: Store) -> dict:
    raw = meta_get(store, _BUDGET_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pop_budget_events(store: Store, snapshot) -> list[dict]:
    """Превышения бюджетов задач → budget_soft (80%) / budget_hard (100%).

    Уровень хранится в meta (escalation только вверх), повторного
    события при каждом poll нет. Пороги — из task.budget_go/budget_usd.
    """
    fired = _budget_levels(store)
    dirty = False
    out: list[dict] = []
    for t in (getattr(snapshot, "tasks", None) or []):
        row = store.get_task(str(t.id))
        if row is None:
            continue
        try:
            lim_go = float(row.get("budget_go") or 0)
        except (TypeError, ValueError):
            lim_go = 0.0
        try:
            lim_usd = float(row.get("budget_usd") or 0)
        except (TypeError, ValueError):
            lim_usd = 0.0
        spent_go = float(getattr(t, "cost_go", 0) or 0)
        spent_usd = float(getattr(t, "cost_usd", 0) or 0)
        level = None
        if (lim_go > 0 and spent_go >= lim_go) or (lim_usd > 0 and spent_usd >= lim_usd):
            level = "hard"
        elif ((lim_go > 0 and spent_go >= 0.8 * lim_go)
                or (lim_usd > 0 and spent_usd >= 0.8 * lim_usd)):
            level = "soft"
        if level is None:
            continue
        prev_level = fired.get(str(t.id))
        rank = {"soft": 1, "hard": 2}
        if prev_level is not None and rank.get(str(prev_level), 0) >= rank[level]:
            continue
        fired[str(t.id)] = level
        dirty = True
        kind = "budget_hard" if level == "hard" else "budget_soft"
        out.append({"task_id": str(t.id), "kind": kind,
                    "payload": {"text": f"go ${spent_go:.2f}/${lim_go:.2f}"
                                f" usd ${spent_usd:.2f}/${lim_usd:.2f}"}})
    if dirty:
        meta_set(store, _BUDGET_KEY, json.dumps(fired, ensure_ascii=False))
    return out


def snapshot_key(snapshot) -> str:
    """Нормализованный JSON снимка без now_ms — для «если что-то менялось»."""
    try:
        data = json.loads(snapshot.to_json())
    except (TypeError, ValueError, AttributeError):
        return str(snapshot)
    if isinstance(data, dict):
        data.pop("now_ms", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def sent_question_ids(store: Store) -> set[int]:
    """Какие вопросы уже слал бот (meta.tg_sent_questions, JSON)."""
    raw = meta_get(store, "tg_sent_questions")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    out: set[int] = set()
    for x in data:
        try:
            out.add(int(x))
        except (TypeError, ValueError):
            continue
    return out


def mark_question_sent(store: Store, qid: int) -> None:
    """Запомнить, что вопрос уже отправлен в TG."""
    seen = sent_question_ids(store) | {int(qid)}
    meta_set(store, "tg_sent_questions", json.dumps(sorted(seen)[-1000:]))


def new_questions_to_send(store: Store) -> list[dict]:
    """Открытые вопросы, ещё не отправленные ботом."""
    sent = sent_question_ids(store)
    return [q for q in list_open_questions(store) if int(q["id"]) not in sent]


def format_roster(snapshot) -> str:
    """Модель → роль → задача → этап → пульс → $ + итог и лимит Go."""
    base = str(snapshot.roster_text())
    footer = (
        f"Итого сегодня: go ${float(snapshot.total_go):.3f}"
        f" + usd ${float(snapshot.total_usd):.3f}"
        f" (лимит Go ${GO_LIMIT:.0f})"
    )
    return _wrap_pre(f"{base}\n{footer}".strip(), MSG_LIMIT)


def format_status(snapshot) -> str:
    """Компактная картина (= hub status) в <pre>."""
    return _wrap_pre(str(snapshot.to_text()), MSG_LIMIT)


def format_task(store: Store, task_id: str, limit: int = TASK_LIMIT,
                snapshot=None) -> str:
    """Этап, сессии, $, замечания (findings H04), ≤ limit символов.

    $ — потрачено из snapshot (TaskSnap.cost_go/cost_usd + по сессиям),
    без snapshot — бюджет задачи. Итоговая длина ≤ limit, </pre> цел.
    """
    tid = str(task_id or "").strip()
    if not tid:
        return "Нужен ID: /task ID"
    task = store.get_task(tid)
    if task is None:
        # Plain-text, но уходит с parse_mode=HTML — экранируем id.
        return esc(f"нет задачи {tid}")
    sessions = store.list_sessions(tid)
    stage = str(task.get("stage") or "")
    rnd = str(task.get("round") or 0)
    try:
        lim_go = float(task.get("budget_go") or 0)
    except (TypeError, ValueError):
        lim_go = 0.0
    try:
        lim_usd = float(task.get("budget_usd") or 0)
    except (TypeError, ValueError):
        lim_usd = 0.0
    tsnap = None
    if snapshot is not None:
        for t in (getattr(snapshot, "tasks", None) or []):
            if str(getattr(t, "id", "")) == tid:
                tsnap = t
                break
    lines = [f"{tid} {stage} r{rnd}"]
    if tsnap is not None:
        lines.append(f"$ go ${float(tsnap.cost_go):.3f}"
                     f" + usd ${float(tsnap.cost_usd):.3f}"
                     f" (бюджет go ${lim_go:.2f} usd ${lim_usd:.2f})")
    else:
        lines.append(f"$ бюджет go ${lim_go:.2f} usd ${lim_usd:.2f}")
    if sessions:
        cost_by_ext: dict[str, float] = {}
        if tsnap is not None:
            for s in (getattr(tsnap, "sessions", None) or []):
                try:
                    cost_by_ext[str(s.external_id)] = float(s.cost)
                except (TypeError, ValueError, AttributeError):
                    continue
        lines.append("Сессии:")
        for s in sessions[:6]:
            role = str(s.get("role") or "?")
            model = str(s.get("model") or "?")
            ext = str(s.get("external_id") or "")
            if ext in cost_by_ext:
                lines.append(f"- {role} {model} ({ext}) ${cost_by_ext[ext]:.3f}")
            else:
                lines.append(f"- {role} {model} ({ext})")
    else:
        lines.append("Сессии: —")
    notes = _task_findings_text(task)
    if notes:
        lines.append("Замечания:")
        lines.append(notes)
    return _wrap_pre("\n".join(lines), int(limit))


def _task_findings_text(task: dict) -> str:
    """Замечания ревью по worktree задачи, пусто если нечего показать."""
    wt = str(task.get("worktree") or "")
    if not wt:
        return ""
    try:
        from pathlib import Path

        from hub.read.findings import (
            dedup_findings,
            format_findings,
            load_findings,
        )

        path = Path(wt)
        if not path.is_dir():
            return ""
        items = dedup_findings(load_findings(path))
        if not items:
            return ""
        return format_findings(items, limit=1500)
    except (OSError, ValueError):
        return ""


def confirm_text(action: str, task_id: str) -> str:
    """Вопрос двухшагового подтверждения /stop и /merge."""
    return f"Подтвердить {action} {task_id}?"


def confirm_buttons(action: str, task_id: str) -> list[Button]:
    """Кнопки Да/Нет для подтверждения."""
    act = str(action or "").strip()
    tid = str(task_id or "").strip()
    return [
        Button(label="✅ Да", data=f"confirm:{act}:{tid}:yes"),
        Button(label="❌ Нет", data=f"confirm:{act}:{tid}:no"),
    ]


def parse_confirm(data: str) -> tuple[str, str, bool] | None:
    """Разобрать «confirm:<action>:<task_id>:<yes|no>»."""
    parts = str(data or "").split(":")
    if len(parts) != 4 or parts[0] != "confirm":
        return None
    _, action, task_id, verdict = parts
    if action not in ("stop", "merge") or not task_id:
        return None
    if verdict == "yes":
        return action, task_id, True
    if verdict == "no":
        return action, task_id, False
    return None


def apply_confirm(store: Store, action: str, task_id: str,
                  ok: bool, now_ms: int) -> str:
    """Применить подтверждение: без Да ничего не пишет.

    spec §9: /merge только для ready; /stop — для нефинальной задачи.
    До pipeline H06 действие — событие owner_command в store.
    """
    if not ok:
        return "Отменено."
    tid = str(task_id or "").strip()
    act = str(action or "").strip()
    if act not in ("stop", "merge"):
        return "Не знаю действия."
    task = store.get_task(tid)
    if task is None:
        return f"нет задачи {tid}"
    stage = str(task.get("stage") or "")
    if act == "merge" and stage != "ready":
        return f"/merge только для ready (сейчас: {stage})."
    if act == "stop" and stage in _FINAL_STAGES:
        return f"Задача уже финальная: {stage}."
    _add_event(store, tid, "owner_command", {"action": act}, int(now_ms))
    return f"✅ {act} {tid} — передано Claude"


def format_budget(store: Store) -> str:
    """Лимиты из таблицы budget + флаг паузы."""
    con = _con(store)
    try:
        rows = con.execute(
            "SELECT scope, key, go_limit, usd_limit, hard FROM budget"
            " ORDER BY scope, key").fetchall()
        items = [dict(r) for r in rows]
    finally:
        con.close()
    lines = ["Бюджет:"]
    if not items:
        lines.append("(пусто)")
    for b in items[:20]:
        hard = "hard" if int(b.get("hard") or 0) else "soft"
        lines.append(
            f"- {b.get('scope')}/{b.get('key')}:"
            f" go {float(b.get('go_limit') or 0):.2f}"
            f" usd {float(b.get('usd_limit') or 0):.2f} {hard}")
    lines.append(f"Очередь: {'пауза' if is_paused(store) else 'идёт'}")
    lines.append(f"Лимит Go: ${GO_LIMIT:.0f}/мес")
    return _wrap_pre(chr(10).join(lines), MSG_LIMIT)


def is_paused(store: Store) -> bool:
    """Флаг meta.queue_paused."""
    return (meta_get(store, "queue_paused") or "") == "1"


def set_paused(store: Store, paused: bool) -> str:
    """Поставить/снять паузу очереди."""
    meta_set(store, "queue_paused", "1" if paused else "0")
    return "⏸ Очередь на паузе" if paused else "▶️ Очередь запущена"


def split_command(text: str) -> tuple[str, str]:
    """Разобрать «/cmd arg …» → (cmd, arg). Без слэша — ('', text)."""
    s = str(text or "").strip()
    if not s.startswith("/"):
        return "", s
    head, _, rest = s[1:].partition(" ")
    cmd = head.split("@")[0].strip().lower()
    return cmd, rest.strip()
