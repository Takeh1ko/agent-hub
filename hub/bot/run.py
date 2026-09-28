"""Тонкая обвязка aiogram: сеть здесь, логика — в hub.bot.core.

Блокирующий код (sqlite, snapshot) — только в sync-хелперах _sync_*,
из event loop они зовутся через asyncio.to_thread. Шаги циклов
(outbox_once/poll_once) — без sleep, тестируются с фейковым ботом.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from hub.bot import core as bc

OUTBOX_S = 5
POLL_S = 15
ROSTER_S = 60 * 60

# bot_msg_id → question_id для ответов реплаем (только event loop трогает).
_QMSG: dict[tuple[int, int], int] = {}
_QMSG_MAX = 1000

# path → настроен ли хендлер (изоляция тестов с разным HOME).
_log_ready: set[str] = set()


def _log_path() -> Path:
    """Путь лога, HOME читается при вызове (тесты подменяют HOME)."""
    return Path.home() / ".local/state/agent-hub/bot.log"


def setup_logging() -> logging.Logger:
    """Лог в ~/.local/state/agent-hub/bot.log, ротация 5×1 МБ."""
    path = _log_path()
    key = str(path)
    logger = logging.getLogger("hub.bot")
    logger.setLevel(logging.INFO)
    if key not in _log_ready:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            key, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        _log_ready.add(key)
    return logger


def get_proxy() -> str | None:
    """Прокси из HTTPS_PROXY (на ПК Telegram только через VPN)."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def _markup(buttons: list[bc.Button]):
    """Кнопки core → InlineKeyboardMarkup (чисто, без сети/БД)."""
    if not buttons:
        return None
    from aiogram.types import InlineKeyboardButton

    rows = [[InlineKeyboardButton(text=b.label, callback_data=b.data)]
            for b in buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _remember_qmsg(chat_id: int, bot_msg_id: int, qid: int) -> None:
    """Запомнить соответствие, с капой от утечки."""
    key = (int(chat_id), int(bot_msg_id))
    _QMSG[key] = int(qid)
    while len(_QMSG) > _QMSG_MAX:
        _QMSG.pop(next(iter(_QMSG)))


def _forget_qid(qid: int) -> None:
    """Убрать отвеченный вопрос из mapping."""
    for key in [k for k, v in _QMSG.items() if v == int(qid)]:
        _QMSG.pop(key, None)


def _resolve_qid(chat_id: int, replied_msg_id: int,
                 replied_text: str) -> int | None:
    """qid по mapping, иначе fallback — парсинг «Вопрос #id» из текста."""
    hit = _QMSG.get((int(chat_id), int(replied_msg_id)))
    if hit is not None:
        return hit
    return bc.parse_question_ref(replied_text or "")


@dataclass
class BotState:
    """Память циклов: дедуп, diff-база, per-chat доставки."""

    grouper: bc.Grouper = field(default_factory=bc.Grouper)
    prev_snap: object = None
    last_roster_norm: str | None = None
    last_roster_ts: int = 0
    outbox_done: set[tuple[int, int]] = field(default_factory=set)
    q_done: set[tuple[int, int]] = field(default_factory=set)


# --- sync-хелперы: вызываются только через asyncio.to_thread ---

def _sync_remember(chat_id: int, now_ms: int) -> None:
    from hub.store import Store

    bc.remember_chat(Store(), int(chat_id), int(now_ms))


def _sync_owner_text(chat_id: int, text: str, now_ms: int) -> str:
    from hub.store import Store

    return bc.handle_owner_text(Store(), int(chat_id), str(text), int(now_ms))


def _sync_all_chats() -> list[int]:
    from hub.store import Store

    return bc.all_chats(Store())


def _sync_outbox_rows() -> list[dict]:
    from hub.store import Store

    return bc.fetch_outbox(Store())


def _sync_outbox_mark(oid: int, now_ms: int) -> None:
    from hub.store import Store

    bc.mark_outbox_sent(Store(), int(oid), int(now_ms))


def _opencode_db() -> str | None:
    db = Path.home() / ".local/share/opencode/opencode.db"
    return str(db) if db.exists() else None


def _sync_status(now_ms: int) -> str:
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    s = snap.build(store, int(now_ms),
                   opencode_db=_opencode_db(), proc_root="/proc")
    return bc.format_status(s)


def _sync_roster(now_ms: int) -> str:
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    s = snap.build(store, int(now_ms),
                   opencode_db=_opencode_db(), proc_root="/proc")
    return bc.format_roster(s)


def _sync_task(task_id: str, now_ms: int) -> str:
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    s = snap.build(store, int(now_ms),
                   opencode_db=_opencode_db(), proc_root="/proc")
    return bc.format_task(store, str(task_id), snapshot=s)


def _sync_budget() -> str:
    from hub.store import Store

    return bc.format_budget(Store())


def _sync_pause(paused: bool) -> str:
    from hub.store import Store

    return bc.set_paused(Store(), bool(paused))


def _sync_confirm(action: str, task_id: str, ok: bool, now_ms: int) -> str:
    from hub.store import Store

    return bc.apply_confirm(Store(), str(action), str(task_id),
                            bool(ok), int(now_ms))


def _sync_answer(qid: int, text: str, now_ms: int) -> tuple[bool, str, str]:
    """(ок, короткий ответ, полный текст для правки вопроса)."""
    from hub.store import Store

    store = Store()
    ok = bc.answer_question(store, int(qid), str(text),
                            via="tg", now_ms=int(now_ms))
    if not ok:
        return False, "Уже отвечен.", ""
    row = bc.get_question(store, int(qid))
    full = bc.format_answered_text(row, str(text)) if row else "✅ Ответ записан"
    return True, "✅ Ответ записан", full


def _sync_answer_cb(data: str, now_ms: int) -> tuple[bool, str]:
    from hub.store import Store

    return bc.answer_by_callback(Store(), str(data), via="tg",
                                 now_ms=int(now_ms))


def _sync_events_take() -> list[dict]:
    from hub.store import Store

    return bc.fetch_unsent_notifiable(Store())


def _sync_events_mark(ids: list[int]) -> None:
    from hub.store import Store

    bc.mark_events_sent(Store(), [int(i) for i in ids])


def _sync_new_questions() -> list[dict]:
    from hub.store import Store

    return bc.new_questions_to_send(Store())


def _sync_q_mark(qid: int) -> None:
    from hub.store import Store

    bc.mark_question_sent(Store(), int(qid))


def _sync_tick(prev_snap, now_ms: int):
    """Продюсер сводок: snapshot.build → дифф → события в store.

    Возвращает (cur_snap, добавленные_строки_с_id). Вызывается в потоке.
    """
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    cur = snap.build(store, int(now_ms),
                     opencode_db=_opencode_db(), proc_root="/proc")
    produced = bc.snapshot_events(prev_snap, cur)
    produced += bc.pop_budget_events(store, cur)
    added = bc.record_events(store, produced, int(now_ms)) if produced else []
    return cur, added


# --- шаги циклов без sleep (тестируются с фейковым ботом) ---

async def outbox_once(bot, state: BotState, now_ms: int) -> int:
    """Одна итерация outbox: каждому чату независимо, без дублей.

    Возвращает число строк, полностью доставленных на этом проходе.
    """
    log = logging.getLogger("hub.bot")
    rows = await asyncio.to_thread(_sync_outbox_rows)
    if not rows:
        return 0
    chats = await asyncio.to_thread(_sync_all_chats)
    done_rows = 0
    for row in rows:
        oid = int(row["id"])
        text = bc.clip(str(row.get("text") or ""))
        if not text.strip():
            await asyncio.to_thread(_sync_outbox_mark, oid, now_ms)
            state.outbox_done = {(o, c) for o, c in state.outbox_done if o != oid}
            done_rows += 1
            continue
        for chat in chats:
            if (oid, int(chat)) in state.outbox_done:
                continue
            try:
                await bot.send_message(int(chat), text)
            except Exception as e:  # noqa: BLE001 — ретрай только этому чату
                log.warning("outbox %s → %s: %s", oid, chat, e)
                continue
            state.outbox_done.add((oid, int(chat)))
        if all((oid, int(c)) in state.outbox_done for c in chats):
            await asyncio.to_thread(_sync_outbox_mark, oid, now_ms)
            state.outbox_done = {(o, c) for o, c in state.outbox_done if o != oid}
            done_rows += 1
    return done_rows


async def poll_once(bot, state: BotState, now_ms: int) -> dict:
    """Одна итерация 15 с: вопросы, продюсер, группировка, авторостер."""
    from hub import time as ht  # noqa: F401 — время идёт параметром now_ms

    log = logging.getLogger("hub.bot")
    res = {"questions": 0, "added": 0, "sent": 0, "roster": False}
    # 1. Продюсер: снимок → разница → события.
    cur, added = await asyncio.to_thread(_sync_tick, state.prev_snap, now_ms)
    state.prev_snap = cur
    res["added"] = len(added)
    # 2. Вопросы новым чатам — per-chat, без дублей получившим.
    for q in await asyncio.to_thread(_sync_new_questions):
        qid = int(q["id"])
        text, buttons = bc.format_question(q)
        chats = await asyncio.to_thread(_sync_all_chats)
        markup = _markup(buttons)
        for chat in chats:
            if (qid, int(chat)) in state.q_done:
                continue
            try:
                sent = await bot.send_message(
                    int(chat), bc.clip(text), reply_markup=markup)
                _remember_qmsg(int(chat), int(sent.message_id), qid)
            except Exception as e:  # noqa: BLE001 — ретрай этому чату
                log.warning("вопрос %s → %s: %s", qid, chat, e)
                continue
            state.q_done.add((qid, int(chat)))
        if all((qid, int(c)) in state.q_done
               for c in await asyncio.to_thread(_sync_all_chats)):
            await asyncio.to_thread(_sync_q_mark, qid)
            state.q_done = {(q_, c) for q_, c in state.q_done if q_ != qid}
            res["questions"] += 1
    # 3. События в группировку (дедуп seen), flush только после успеха.
    for ev in added:
        state.grouper.add(ev, now_ms)
    for ev in await asyncio.to_thread(_sync_events_take):
        state.grouper.add(ev, now_ms)
    if state.grouper.buf and state.grouper.ready(now_ms):
        text = state.grouper.peek()
        ids = state.grouper.pending_ids()
        if text.strip():
            chats = await asyncio.to_thread(_sync_all_chats)
            ok_any = False
            for chat in chats:
                try:
                    await bot.send_message(int(chat), bc.clip(text))
                    ok_any = True
                except Exception as e:  # noqa: BLE001 — best-effort сводка
                    log.warning("сводка → %s: %s", chat, e)
                    continue
            if ok_any:
                await asyncio.to_thread(_sync_events_mark, ids)
                state.grouper.clear()
                state.grouper.drop(ids)
                res["sent"] = len(ids)
    # 4. Авторостер раз в час, если нормализованный снимок менялся.
    norm = bc.snapshot_key(cur)
    if state.last_roster_norm is None:
        state.last_roster_norm = norm
        state.last_roster_ts = now_ms
    elif (now_ms - state.last_roster_ts >= ROSTER_S * 1000
            and norm != state.last_roster_norm):
        roster = bc.format_roster(cur)
        for chat in await asyncio.to_thread(_sync_all_chats):
            try:
                await bot.send_message(int(chat), roster, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001
                log.warning("авторостер → %s: %s", chat, e)
                continue
        state.last_roster_norm = norm
        state.last_roster_ts = now_ms
        res["roster"] = True
    return res


# --- обвязка ---

def build_dispatcher() -> Dispatcher:
    """Собрать Dispatcher без сети и файлов (для --dry-run и тестов)."""
    router = Router()

    @router.message(Command("start"))
    async def _start(msg: Message) -> None:
        from hub import time as ht

        chat = int(msg.chat.id)
        await asyncio.to_thread(_sync_remember, chat, ht.now_ms())
        await msg.answer(bc.START_TEXT)

    @router.message(Command("help"))
    async def _help(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        await msg.answer(bc.HELP_TEXT)

    @router.message(Command("status"))
    async def _status(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_status, ht.now_ms())
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("roster"))
    async def _roster(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_roster, ht.now_ms())
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("task"))
    async def _task(msg: Message) -> None:
        from hub import time as ht

        now = ht.now_ms()
        await asyncio.to_thread(_sync_remember, int(msg.chat.id), now)
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /task ID")
            return
        tid = arg.split()[0]
        text = await asyncio.to_thread(_sync_task, tid, now)
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("stop"))
    async def _stop(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /stop ID")
            return
        tid = arg.split()[0]
        await msg.answer(bc.confirm_text("stop", tid),
                         reply_markup=_markup(bc.confirm_buttons("stop", tid)))

    @router.message(Command("merge"))
    async def _merge(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /merge ID")
            return
        tid = arg.split()[0]
        await msg.answer(bc.confirm_text("merge", tid),
                         reply_markup=_markup(bc.confirm_buttons("merge", tid)))

    @router.message(Command("budget"))
    async def _budget(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_budget)
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("pause"))
    async def _pause(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_pause, True)
        await msg.answer(text)

    @router.message(Command("resume"))
    async def _resume(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_pause, False)
        await msg.answer(text)

    @router.callback_query(F.data.startswith("qans:"))
    async def _qans(call: CallbackQuery) -> None:
        from hub import time as ht

        ok, text = await asyncio.to_thread(
            _sync_answer_cb, str(call.data or ""), ht.now_ms())
        parsed = bc.parse_answer_callback(str(call.data or ""))
        if ok and parsed is not None:
            _forget_qid(parsed[0])
        try:
            if ok:
                await call.message.edit_text(bc.clip(text))
            await call.answer("✅ Записано" if ok else text)
        except Exception:  # noqa: BLE001 — правка может устареть
            await call.answer("✅ Записано" if ok else text)

    @router.callback_query(F.data.startswith("confirm:"))
    async def _confirm(call: CallbackQuery) -> None:
        from hub import time as ht

        parsed = bc.parse_confirm(str(call.data or ""))
        if parsed is None:
            await call.answer("Не понял кнопку.")
            return
        action, tid, ok = parsed
        text = await asyncio.to_thread(
            _sync_confirm, action, tid, ok, ht.now_ms())
        try:
            await call.message.edit_text(bc.clip(text))
        except Exception:  # noqa: BLE001 — сообщение могли удалить
            await call.message.answer(bc.clip(text))
        await call.answer("Готово")

    @router.message(F.text)
    async def _text(msg: Message) -> None:
        from hub import time as ht

        body = str(msg.text or "")
        chat = int(msg.chat.id)
        now = ht.now_ms()
        if body.startswith("/"):
            await asyncio.to_thread(_sync_remember, chat, now)
            await msg.answer("Не знаю команды. /help — список.")
            return
        # Ответ реплаем на вопрос бота (mapping или «Вопрос #id» в тексте).
        qid: int | None = None
        replied = getattr(msg, "reply_to_message", None)
        replied_id = 0
        if replied is not None:
            replied_id = int(getattr(replied, "message_id", 0) or 0)
            qid = _resolve_qid(chat, replied_id,
                               str(getattr(replied, "text", "") or ""))
        if qid is not None:
            ok, short, full = await asyncio.to_thread(
                _sync_answer, qid, body, now)
            if ok:
                _forget_qid(qid)
                if replied is not None:
                    try:
                        await msg.bot.edit_message_text(
                            text=bc.clip(full), chat_id=chat,
                            message_id=replied_id)
                    except Exception:  # noqa: BLE001 — best-effort правка
                        pass
            await msg.answer(bc.clip(short if ok else full or short))
            return
        text = await asyncio.to_thread(_sync_owner_text, chat, body, now)
        await msg.answer(bc.clip(text))

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def _outbox_loop(bot) -> None:
    """Раз в 5 с слать outbox (шаг — outbox_once)."""
    from hub import time as ht

    log = logging.getLogger("hub.bot")
    state = BotState()
    while True:
        await asyncio.sleep(OUTBOX_S)
        try:
            await outbox_once(bot, state, ht.now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл не умирает
            log.warning("outbox-цикл: %s", e)


async def _poll_loop(bot) -> None:
    """Раз в 15 с: вопросы, продюсер, группировка, авторостер."""
    from hub import time as ht

    log = logging.getLogger("hub.bot")
    state = BotState()
    while True:
        await asyncio.sleep(POLL_S)
        try:
            await poll_once(bot, state, ht.now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл не умирает
            log.warning("poll-цикл: %s", e)


async def _amain() -> None:
    """Запустить polling + фоны, падение сети — повтор с паузой."""
    from hub.secrets import TELEGRAM_BOT_TOKEN

    log = setup_logging()
    proxy = get_proxy()
    if proxy:
        from aiogram.client.session.aiohttp import AiohttpSession

        session = AiohttpSession(proxy=proxy)
    else:
        session = None
    from aiogram import Bot

    bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
    dp = build_dispatcher()
    tasks = [asyncio.create_task(_outbox_loop(bot)),
             asyncio.create_task(_poll_loop(bot))]
    try:
        while True:
            try:
                await dp.start_polling(bot)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — процесс не умирает
                log.warning("polling упал, повтор через 5 с: %s", e)
                await asyncio.sleep(5)
    finally:
        for t in tasks:
            t.cancel()
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001 — закрытие best-effort
            pass


def main() -> int:
    """Точка входа hub bot (блокирует до Ctrl-C)."""
    asyncio.run(_amain())
    return 0
