"""TG-бот v2: aiogram 3, long polling через HTTPS_PROXY. Логика — ahub.tg.core, запуск Claude — ahub.tg.launcher.

Блокирующее (sqlite, /proc) — через asyncio.to_thread. Фоновые циклы: исходящие Claude (outbox), вопросы,
тревоги наблюдателя, надзор за запущенным Claude.
"""

from __future__ import annotations

import asyncio
import os
import sys

from ahub import comms, config
from ahub import log as hublog
from ahub.i18n import t as _t
from ahub.store import Store
from ahub.tg import core
from ahub.tg import launcher

LOOP_S = 5
LAUNCH_S = 10
HEARTBEAT_KEY = "tg_heartbeat"
_log = hublog.get("tg")
_QMSG: dict[tuple[int, int], int] = {}  # (chat, bot_msg_id) → question_id — ответ реплаем


def _markup(rows):
    if not rows:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b.label, callback_data=b.data)
                                                  for b in row] for row in rows])


def build_session(hub: config.HubConfig | None = None):
    """Сессия бота: прокси из [telegram] вместо системного; без прокси — None."""
    cfg_proxy = ""
    if hub is None:
        try:
            hub = config.load_hub()
        except config.ConfigError:
            hub = None
    if hub is not None:
        cfg_proxy = (hub.tg_proxy or "").strip()
    proxy = cfg_proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not proxy:
        return None
    from ahub.tg.proxy import HttpProxySession

    return HttpProxySession(proxy)


async def _send(bot, store: Store, reply: core.Reply) -> list[tuple[int, int]]:
    sent = []
    for chat in await asyncio.to_thread(core.chats, store):
        try:
            m = await bot.send_message(chat, core.clip(reply.text), reply_markup=_markup(reply.buttons))
            sent.append((chat, m.message_id))
        except Exception as e:  # чат мог заблокировать бота
            _log.warning("TG %s: %s", chat, e)
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                await asyncio.to_thread(core.mark_dead, store, chat)
    return sent


async def background(bot, store: Store) -> None:
    last_launch = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            for m in await asyncio.to_thread(comms.outbox, store):
                if await _send(bot, store, core.Reply(m["text"])):
                    await asyncio.to_thread(comms.mark_sent, store, m["id"])
            for q in await asyncio.to_thread(core.pending_questions, store):
                for key in await _send(bot, store, core.question_reply(q)):
                    _QMSG[key] = q["id"]
                await asyncio.to_thread(core.mark_question_sent, store, q["id"])
            alarms = await asyncio.to_thread(comms.alarms_for_tg, store)
            for e in alarms:
                await _send(bot, store, core.Reply(core.alarm_text(e)))
            await asyncio.to_thread(comms.mark_tg_sent, store, [e.id for e in alarms])
            if loop.time() - last_launch >= LAUNCH_S:
                last_launch = loop.time()
                res = await asyncio.to_thread(launcher.tick, store)
                if res == "limit":
                    _log.warning("лимит запусков Claude в час исчерпан")
            await asyncio.to_thread(store.meta_set, HEARTBEAT_KEY, str(int(loop.time())))
        except Exception:
            _log.exception("фоновый цикл бота упал")
        await asyncio.sleep(LOOP_S)


def build_dispatcher(store: Store):
    from aiogram import Dispatcher, F, Router
    from aiogram.filters import Command
    from aiogram.types import CallbackQuery, Message

    r = Router()

    def _projects() -> list[str]:
        ps, _ = config.load_projects()
        return [p.name for p in ps]

    @r.message(Command("start", "help"))
    async def _help(msg: Message) -> None:
        await asyncio.to_thread(core.remember_chat, store, msg.chat.id)
        await msg.answer(core.help_text())

    @r.message(Command("status"))
    async def _status(msg: Message) -> None:
        await msg.answer(core.clip(await asyncio.to_thread(core.status_text, store)))

    @r.message(Command("tasks"))
    async def _tasks(msg: Message) -> None:
        rep = await asyncio.to_thread(core.tasks_reply, store)
        await msg.answer(rep.text, reply_markup=_markup(rep.buttons))

    @r.callback_query(F.data.startswith("task:") | (F.data == "tasks"))
    async def _task(call: CallbackQuery) -> None:
        if call.data == "tasks":
            rep = await asyncio.to_thread(core.tasks_reply, store)
        else:
            rep = await asyncio.to_thread(core.task_detail, store, int(call.data.split(":")[1]))
        await call.message.edit_text(rep.text, reply_markup=_markup(rep.buttons))
        await call.answer()

    @r.callback_query(F.data.startswith("ans:"))
    async def _ans(call: CallbackQuery) -> None:
        text = await asyncio.to_thread(core.on_answer_button, store, call.data)
        await call.message.edit_text(text)
        await call.answer()

    @r.message(F.text)
    async def _text(msg: Message) -> None:
        if msg.reply_to_message is not None:
            qid = _QMSG.get((msg.chat.id, msg.reply_to_message.message_id))
            if qid is not None:
                await msg.answer(await asyncio.to_thread(core.on_reply_to_question, store, qid, msg.text))
                return
        rep = await asyncio.to_thread(core.on_text, store, msg.chat.id, msg.text, projects=_projects())
        await msg.answer(rep.text)

    dp = Dispatcher()
    dp.include_router(r)
    return dp


async def amain(hub: config.HubConfig | None = None) -> None:
    from aiogram import Bot

    if hub is None:
        hub = config.load_hub()
    store = Store()
    bot = Bot(hub.tg_token, session=build_session(hub))
    dp = build_dispatcher(store)
    task = asyncio.create_task(background(bot, store))
    try:
        while True:
            try:
                await dp.start_polling(bot, handle_signals=False)
                break
            except Exception:
                _log.exception("polling упал — повтор через 15 с")
                await asyncio.sleep(15)
    finally:
        task.cancel()
        await bot.session.close()


def main() -> int:
    hublog.setup()
    hublog.install_excepthook("tg")
    try:
        hub = config.load_hub()
    except config.ConfigError as e:
        print(_t("cli.error", msg=e), file=sys.stderr)
        return 2
    if not hub.tg_token:
        print(_t("tg.err_no_token"), file=sys.stderr)
        return 2
    asyncio.run(amain(hub))
    return 0
