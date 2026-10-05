"""TG bot v2: aiogram 3, long polling via HTTPS_PROXY. Logic — ahub.tg.core, Claude launch — ahub.tg.launcher.

Blocking (sqlite, /proc) goes via asyncio.to_thread. Background loops: Claude outbox, questions,
observer alarms, launched-Claude supervision, the code check.
"""

from __future__ import annotations

import asyncio
import os
import sys

from ahub import comms, config, selfupdate
from ahub import log as hublog
from ahub.i18n import t as _t
from ahub.store import Store
from ahub.tg import core, launcher

LOOP_S = 5
LAUNCH_S = 10
FAIL_MAX = 3  # the same failure in a row — the owner hears about it once, the loop slows down
FAIL_BACKOFF_S = 60.0
_log = hublog.get("tg")


def _markup(rows):
    if not rows:
        return None
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=b.label, callback_data=b.data)
                                                  for b in row] for row in rows])


def build_session(hub: config.HubConfig | None = None):
    """Bot session: [telegram] proxy over the system one; no proxy — None."""
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
        except Exception as e:  # chat may have blocked the bot
            _log.warning("TG %s: %s", chat, e)
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                await asyncio.to_thread(core.mark_dead, store, chat)
    return sent


async def _alarm(bot, store: Store, err: str) -> None:
    """The owner-visible alarm of a loop that keeps failing: the store first (Claude, `ahub alarms`, the outbox),
    then the chats by hand — the pass that would deliver it is the one that keeps failing. Nothing here may
    escape: this runs inside the loop's except, and an exception from it kills the loop for good.
    """
    text = _t("tg.alarm_loop", err=err)
    event = None
    try:
        event = await asyncio.to_thread(comms.raise_alarm, store, text, critical=True)
    except Exception as e:
        _log.error("the bot cannot raise its alarm: %s", e)
    try:
        if await _send(bot, store, core.Reply(text)) and event is not None:
            await asyncio.to_thread(comms.mark_tg_sent, store, [event])  # sent by hand — not again by the outbox
    except Exception as e:
        _log.error("the bot cannot send its alarm: %s", e)


async def background(bot, store: Store) -> None:
    last_launch = 0.0
    loop = asyncio.get_running_loop()
    code0: str | None = None  # None — not read yet (or the read failed); the pass still runs
    fp_warned = False
    last_code_check = loop.time()
    fail_n, reported = 0, False
    while True:
        try:
            if code0 is None:
                try:
                    code0 = await asyncio.to_thread(selfupdate.code_fingerprint)
                except OSError:
                    if not fp_warned:  # once — then a quiet retry on every tick until it works
                        fp_warned = True
                        _log.exception("the bot's code fingerprint failed — retry on the next pass")
                else:
                    fp_warned = False
            for m in await asyncio.to_thread(comms.outbox, store):
                if await _send(bot, store, core.Reply(m["text"])):
                    await asyncio.to_thread(comms.mark_sent, store, m["id"])
            for q in await asyncio.to_thread(core.pending_questions, store):
                for chat, mid in await _send(bot, store, core.question_reply(q)):
                    await asyncio.to_thread(core.remember_question_message, store, chat, mid, q["id"])
                await asyncio.to_thread(core.mark_question_sent, store, q["id"])
            alarms = await asyncio.to_thread(comms.alarms_for_tg, store)
            for e in alarms:
                await _send(bot, store, core.Reply(core.alarm_text(e)))
            await asyncio.to_thread(comms.mark_tg_sent, store, [e.id for e in alarms])
            if loop.time() - last_launch >= LAUNCH_S:
                last_launch = loop.time()
                res = await asyncio.to_thread(launcher.tick, store)  # nodir dedupe lives in meta (a re-exec keeps it)
                if res == "limit":
                    _log.warning("Claude launch hourly limit exhausted")
                elif res.startswith("nodir:"):  # the launcher says it once — the owner hears it once
                    name = res.split(":", 1)[1]
                    await _send(bot, store, core.Reply(_t("tg.launch_no_dir", name=name) if name
                                                       else _t("tg.launch_no_dir_hub")))
            fail_n, reported = 0, False
        except Exception as e:
            fail_n += 1  # the kind is in the log and the alarm: a loop that alternates two kinds is still failing
            if fail_n < FAIL_MAX:
                _log.exception("bot background loop crashed (%d/%d: %s)", fail_n, FAIL_MAX, type(e).__name__)
            elif not reported:  # failing for minutes: one log line, one alarm, then a slow retry
                reported = True
                _log.exception("bot background loop failed %d times in a row (%s) — retry in %d s",
                               fail_n, type(e).__name__, FAIL_BACKOFF_S)
                await _alarm(bot, store, f"{type(e).__name__}: {str(e)[:200]}")
        if code0 is not None and loop.time() - last_code_check >= selfupdate.CODE_CHECK_S:
            # outside the try, like the service's: a pass that keeps failing is exactly when new code is wanted
            last_code_check = loop.time()
            try:
                code = await asyncio.to_thread(selfupdate.code_fingerprint)
                if code != code0:  # a merge is in — the new code runs from here, not from the next restart
                    ok, why = await asyncio.to_thread(selfupdate.new_code_healthy)
                    if ok:
                        _log.info("hub code changed — the bot restarts on it (the messages of this pass are sent)")
                        selfupdate.restart_self()
                    else:
                        _log.error("hub code changed but fails check — staying on old: %s", why)
                        code0 = code  # do not re-check every CODE_CHECK_S; the next change is checked again
            except Exception:
                _log.exception("the bot's code check failed — the next pass tries again")
        await asyncio.sleep(FAIL_BACKOFF_S if reported else LOOP_S)


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

    @r.message(Command("project"))
    async def _project(msg: Message) -> None:
        arg = " ".join((msg.text or "").split()[1:]).strip()
        names = await asyncio.to_thread(_projects)
        rep = await asyncio.to_thread(core.project_reply, store, names, arg or None, msg.chat.id)
        await msg.answer(rep.text, reply_markup=_markup(rep.buttons))

    @r.callback_query(F.data.startswith("proj:"))
    async def _pick_project(call: CallbackQuery) -> None:
        names = await asyncio.to_thread(_projects)
        rep = await asyncio.to_thread(core.project_reply, store, names, call.data.split(":", 1)[1],
                                      call.message.chat.id)
        await call.message.edit_text(rep.text, reply_markup=_markup(rep.buttons))
        await call.answer()

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
            qid = await asyncio.to_thread(core.take_question_message, store,
                                          msg.chat.id, msg.reply_to_message.message_id)
            if qid is not None:  # the mapping is forgotten with the take — no growth over weeks
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
                _log.exception("polling failed — retry in 15 s")
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
