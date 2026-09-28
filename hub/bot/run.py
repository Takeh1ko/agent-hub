"""Тонкая обвязка aiogram: сеть здесь, логика — в hub.bot.core.

Блокирующий код (sqlite, snapshot) — только в sync-хелперах _sync_*,
из event loop они зовутся через asyncio.to_thread.
"""

from __future__ import annotations

import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from hub.bot import core as bc

def _log_path() -> Path:
    """Путь лога, HOME читается при вызове (тесты подменяют HOME)."""
    return Path.home() / ".local/state/agent-hub/bot.log"


OUTBOX_S = 5
POLL_S = 15
ROSTER_S = 60 * 60

# bot_msg_id → question_id для ответов реплаем (только event loop трогает).
_QMSG: dict[tuple[int, int], int] = {}

_log_setup = False


def setup_logging() -> logging.Logger:
    """Лог в ~/.local/state/agent-hub/bot.log, ротация 5×1 МБ."""
    global _log_setup
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("hub.bot")
    logger.setLevel(logging.INFO)
    if not _log_setup:
        handler = RotatingFileHandler(
            str(path), maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        _log_setup = True
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


def _sync_status(now_ms: int) -> str:
    from hub import time as ht
    from hub.read import snapshot as snap
    from hub.store import Store

    _ = ht  # время передано параметром now_ms
    store = Store()
    db = Path.home() / ".local/share/opencode/opencode.db"
    s = snap.build(store, int(now_ms),
                   opencode_db=str(db) if db.exists() else None,
                   proc_root="/proc")
    return bc.format_status(s)


def _sync_roster(now_ms: int) -> str:
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    db = Path.home() / ".local/share/opencode/opencode.db"
    s = snap.build(store, int(now_ms),
                   opencode_db=str(db) if db.exists() else None,
                   proc_root="/proc")
    return bc.format_roster(s)


def _sync_roster_json(now_ms: int) -> tuple[str, str]:
    """(roster_html, snapshot_json) для автосводки."""
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    db = Path.home() / ".local/share/opencode/opencode.db"
    s = snap.build(store, int(now_ms),
                   opencode_db=str(db) if db.exists() else None,
                   proc_root="/proc")
    return bc.format_roster(s), s.to_json()


def _sync_task(task_id: str) -> str:
    from hub.store import Store

    return bc.format_task(Store(), str(task_id))


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


def _sync_answer(qid: int, text: str, now_ms: int) -> tuple[bool, str]:
    from hub.store import Store

    ok = bc.answer_question(Store(), int(qid), str(text),
                            via="tg", now_ms=int(now_ms))
    if not ok:
        return False, "Уже отвечен."
    row = bc.get_question(Store(), int(qid))
    if row is None:
        return True, "✅ Ответ записан"
    return True, "✅ Ответ записан: " + bc.format_answered_text(row, str(text))


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


# --- обвязка ---

def build_dispatcher() -> Dispatcher:
    """Собрать Dispatcher без сети (для --dry-run и тестов)."""
    setup_logging()
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

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /task ID")
            return
        tid = arg.split()[0]
        text = await asyncio.to_thread(_sync_task, tid)
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
        # Ответ реплаем на вопрос бота?
        qid: int | None = None
        replied = getattr(msg, "reply_to_message", None)
        if replied is not None:
            qid = _QMSG.get((chat, int(getattr(replied, "message_id", 0))))
        if qid is not None:
            ok, text = await asyncio.to_thread(_sync_answer, qid, body, now)
            await msg.answer(bc.clip(text))
            return
        text = await asyncio.to_thread(_sync_owner_text, chat, body, now)
        await msg.answer(bc.clip(text))

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def _outbox_loop(bot) -> None:
    """Раз в 5 с слать outbox, ставить sent_ts."""
    from hub import time as ht

    log = logging.getLogger("hub.bot")
    while True:
        await asyncio.sleep(OUTBOX_S)
        try:
            rows = await asyncio.to_thread(_sync_outbox_rows)
            if not rows:
                continue
            chats = await asyncio.to_thread(_sync_all_chats)
            for row in rows:
                text = bc.clip(str(row.get("text") or ""))
                if not text.strip():
                    await asyncio.to_thread(
                        _sync_outbox_mark, int(row["id"]), ht.now_ms())
                    continue
                ok = True
                for chat in chats:
                    try:
                        await bot.send_message(chat, text)
                    except Exception as e:  # noqa: BLE001 — сеть, ретрай
                        log.warning("outbox %s → %s: %s", row["id"], chat, e)
                        ok = False
                        break
                if ok:
                    await asyncio.to_thread(
                        _sync_outbox_mark, int(row["id"]), ht.now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл не умирает
            log.warning("outbox-цикл: %s", e)


async def _poll_loop(bot) -> None:
    """Раз в 15 с: вопросы, события (группировка 5 мин), авторостер 60 мин."""
    from hub import time as ht

    log = logging.getLogger("hub.bot")
    grouper = bc.Grouper()
    pending_ids: list[int] = []
    last_roster_ts = 0
    last_json: str | None = None
    while True:
        await asyncio.sleep(POLL_S)
        try:
            now = ht.now_ms()
            # Вопросы: новые open → чаты с кнопками.
            for q in await asyncio.to_thread(_sync_new_questions):
                text, buttons = bc.format_question(q)
                chats = await asyncio.to_thread(_sync_all_chats)
                for chat in chats:
                    try:
                        sent = await bot.send_message(
                            chat, bc.clip(text),
                            reply_markup=_markup(buttons))
                        _QMSG[(int(chat), int(sent.message_id))] = int(q["id"])
                    except Exception as e:  # noqa: BLE001 — сеть
                        log.warning("вопрос %s → %s: %s", q["id"], chat, e)
                        break
                else:
                    await asyncio.to_thread(_sync_q_mark, int(q["id"]))
            # События в группировку.
            for ev in await asyncio.to_thread(_sync_events_take):
                grouper.add(ev, now)
                pending_ids.append(int(ev["id"]))
            if grouper.buf and grouper.ready(now):
                ids = list(pending_ids)
                text = grouper.flush()
                pending_ids.clear()
                if text.strip():
                    chats = await asyncio.to_thread(_sync_all_chats)
                    sent_ok = True
                    for chat in chats:
                        try:
                            await bot.send_message(chat, bc.clip(text))
                        except Exception as e:  # noqa: BLE001
                            log.warning("сводка → %s: %s", chat, e)
                            sent_ok = False
                            break
                    if sent_ok:
                        await asyncio.to_thread(_sync_events_mark, ids)
                    else:
                        # Вернуть несброшенное назад — не теряем сводку.
                        for eid in ids:
                            pending_ids.append(eid)
            # Авторостер раз в час при изменениях.
            if now - last_roster_ts >= ROSTER_S * 1000:
                roster, js = await asyncio.to_thread(_sync_roster_json, now)
                if last_json is not None and js != last_json:
                    chats = await asyncio.to_thread(_sync_all_chats)
                    for chat in chats:
                        try:
                            await bot.send_message(
                                chat, roster, parse_mode="HTML")
                        except Exception as e:  # noqa: BLE001
                            log.warning("авторостер → %s: %s", chat, e)
                            break
                last_json = js
                last_roster_ts = now
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
