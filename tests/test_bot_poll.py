"""H05 прод-пути: outbox_once/poll_once с фейковым ботом (без сети)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from hub.bot import core as bc
from hub.bot.run import BotState, outbox_once, poll_once
from hub.store import Store

NOW = 1_789_000_000_000
HOUR = 3_600_000


class FakeBot:
    """Фейковый отправитель: send_message(chat, text, ...) без сети."""

    def __init__(self, fail: set[int] | None = None) -> None:
        self.fail = set(fail or ())
        self.sent: list[tuple[int, str]] = []
        # reply_markup каждого send/edit (None — поле не передавали).
        self.sent_kb: list[object] = []
        self.edited: list[tuple[int, int, str]] = []
        self.edited_kb: list[object] = []
        self.markup_dropped: list[tuple[int, int]] = []
        self.markup_dropped_kb: list[object] = []
        self._mid = 0

    async def send_message(self, chat, text, **kwargs):
        if int(chat) in self.fail:
            raise RuntimeError(f"сеть упала → {chat}")
        self._mid += 1
        self.sent.append((int(chat), str(text)))
        self.sent_kb.append(kwargs.get("reply_markup"))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, text, chat_id=None,
                                message_id=None, **kwargs):
        self.edited.append((int(chat_id), int(message_id), str(text)))
        self.edited_kb.append(kwargs.get("reply_markup"))

    async def edit_message_reply_markup(self, chat_id=None,
                                        message_id=None, **kwargs):
        self.markup_dropped.append((int(chat_id), int(message_id)))
        self.markup_dropped_kb.append(kwargs.get("reply_markup"))

    def texts(self, chat: int | None = None) -> list[str]:
        if chat is None:
            return [t for _, t in self.sent]
        return [t for c, t in self.sent if c == int(chat)]

    def count(self, chat: int | None = None) -> int:
        return len(self.texts(chat))


def _con(store: Store):
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    return con


def _owner() -> int:
    from hub.tg_send import OWNER_CHAT_ID

    return int(OWNER_CHAT_ID)


def test_outbox_prod_exactly_once():
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        con = _con(s)
        try:
            con.execute(
                "INSERT INTO outbox(ts, text, task_id, sent_ts)"
                " VALUES (?, 'привет', '', NULL)", (NOW,))
            con.commit()
        finally:
            con.close()
        bot, state = FakeBot(), BotState()
        assert await outbox_once(bot, state, NOW) == 1
        # Ровно по одному каждому чату, повтор — тишина.
        assert bot.count(555) == 1 and bot.count(_owner()) == 1
        assert await outbox_once(bot, state, NOW + 5000) == 0
        assert bot.count(555) == 1 and bot.count() == 2

    asyncio.run(_go())


def test_outbox_partial_failure_no_dup():
    """Второй чат упал: первому дубль не шлётся, ретрай — только второму."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        con = _con(s)
        try:
            con.execute(
                "INSERT INTO outbox(ts, text, task_id, sent_ts)"
                " VALUES (?, 'важно', '', NULL)", (NOW,))
            con.commit()
        finally:
            con.close()
        bot, state = FakeBot(fail={_owner()}), BotState()
        assert await outbox_once(bot, state, NOW) == 0  # не всем дошло
        assert bot.count(555) == 1 and bot.count(_owner()) == 0
        bot.fail.clear()
        assert await outbox_once(bot, state, NOW + 5000) == 1
        assert bot.count(555) == 1, "первому чату — без дубля"
        assert bot.count(_owner()) == 1
        assert await outbox_once(bot, state, NOW + 10000) == 0

    asyncio.run(_go())


def test_poll_grouping_no_dup_and_mark():
    """Два забора одного события — одна строка в буфере и одна отправка."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.add_event("H01", "ready", {"stage": "ready"})
        bot, state = FakeBot(), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["added"] == 0  # baseline снимка молчит, событие уже в БД
        assert len(state.grouper.buf) == 1
        r2 = await poll_once(bot, state, NOW + 60_000)
        assert len(state.grouper.buf) == 1, "дедуп seen"
        assert bot.count() == 0  # окно 5 мин не вышло
        r3 = await poll_once(bot, state, NOW + 5 * 60_000)
        assert r3["sent"] == 1
        summaries = [t for t in bot.texts() if "H01" in t]
        assert len(summaries) == 2  # по одному каждому чату
        assert all(t.startswith("Сводка (1):") for t in summaries)
        con = _con(s)
        try:
            left = con.execute(
                "SELECT COUNT(*) FROM event WHERE sent_tg=0").fetchone()[0]
        finally:
            con.close()
        assert left == 0
        r4 = await poll_once(bot, state, NOW + 6 * 60_000)
        assert r4["sent"] == 0 and bot.count() == 2

    asyncio.run(_go())


def test_poll_summary_lossless_on_failure():
    """Неуспешная отправка не очищает буфер: повтор шлёт один раз."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.add_event("H02", "failed", {"stage": "failed"})
        bot, state = FakeBot(fail={555, _owner()}), BotState()
        r = await poll_once(bot, state, NOW + 5 * 60_000 + 1)
        # Окно вышло с первого забора? first_ts=NOW+5мин, ready требует
        # +5 мин от first_ts — буфер цел, отправки нет.
        assert r["sent"] == 0 and len(state.grouper.buf) == 1
        r = await poll_once(bot, state, NOW + 10 * 60_000 + 2)
        assert r["sent"] == 0 and len(state.grouper.buf) == 1
        assert bot.count() == 0
        bot.fail.clear()
        r = await poll_once(bot, state, NOW + 10 * 60_000 + 3)
        assert r["sent"] == 1 and state.grouper.buf == []
        assert bot.count() == 2  # по одному каждому, без 20 дублей

    asyncio.run(_go())


def test_poll_snapshot_producer_stage_change():
    """Смена этапа exec→ready между тиками — ровно одно событие в сводке."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.upsert_task(id="H50", stage="exec r1", worktree="")
        bot, state = FakeBot(), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["added"] == 0  # baseline
        s.upsert_task(id="H50", stage="ready")
        r2 = await poll_once(bot, state, NOW + 15_000)
        assert r2["added"] == 1
        assert [e["kind"] for e in state.grouper.buf] == ["ready"]
        r3 = await poll_once(bot, state, NOW + 5 * 60_000 + 30_000)
        assert r3["sent"] == 1
        assert any("H50" in t for t in bot.texts())

    asyncio.run(_go())


def test_poll_roster_only_on_change():
    """Авторостер: без изменений — тишина, с новой задачей — одно сообщение."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        bot, state = FakeBot(), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["roster"] is False  # baseline
        r2 = await poll_once(bot, state, NOW + HOUR + 1)
        assert r2["roster"] is False, "now_ms в to_json не считается изменением"
        assert not [t for t in bot.texts() if "Сегодня: задачи хаба" in t]
        s.upsert_task(id="HN", stage="exec r1", worktree="")
        r3 = await poll_once(bot, state, NOW + 2 * HOUR + 2)
        assert r3["roster"] is True
        rosters = [t for t in bot.texts() if "Сегодня: задачи хаба" in t]
        assert len(rosters) == 2  # каждому чату по одному

    asyncio.run(_go())


def test_poll_questions_prod_path_once():
    """Вопрос уходит каждому чату один раз, повторный тик молчит."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        con = _con(s)
        try:
            con.execute(
                "INSERT INTO question(task_id, asked_by, text, options_json,"
                " status, answer, answered_via, ts)"
                " VALUES ('T1','claude','идём?','[\"да\",\"нет\"]',"
                " 'open','','',?)", (NOW,))
            con.commit()
        finally:
            con.close()
        bot, state = FakeBot(), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["questions"] == 1
        assert bot.count(555) == 1 and bot.count(_owner()) == 1
        assert any("идём?" in t for t in bot.texts())
        r2 = await poll_once(bot, state, NOW + 15_000)
        assert r2["questions"] == 0 and bot.count() == 2

    asyncio.run(_go())


def test_qmsg_fallback_after_restart():
    """Без mapping ответ реплаем резолвится из «Вопрос #id»."""
    from hub.bot import run as br

    br._QMSG.clear()
    br._QSENT.clear()
    assert br._resolve_qid(1, 999, "❓ Вопрос #7 [T1]:\nидём?") == 7
    br._remember_qmsg(1, 10, 5)
    assert br._resolve_qid(1, 10, "что-то") == 5
    br._forget_qid(5)
    assert br._resolve_qid(1, 10, "что-то") is None
    # Капа от утечки.
    for i in range(1200):
        br._remember_qmsg(1, 100 + i, i)
    assert len(br._QMSG) <= 1000
    assert len(br._QSENT) <= 1000
    br._QMSG.clear()
    br._QSENT.clear()


def _add_question(s: Store, text="идём?", opts=None) -> int:
    con = _con(s)
    try:
        cur = con.execute(
            "INSERT INTO question(task_id, asked_by, text, options_json,"
            " status, answer, answered_via, ts)"
            " VALUES ('T1','claude',?,?, 'open','','',?)",
            (text, json.dumps(opts if opts is not None else ["да", "нет"],
                              ensure_ascii=False), NOW),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def test_poll_summary_partial_per_chat_retry():
    """MEDIUM: сводка per-chat — упавший чат добирает ретраем, без дублей."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.add_event("H01", "ready", {"stage": "ready"})
        bot, state = FakeBot(), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["sent"] == 0 and len(state.grouper.buf) == 1
        bot.fail = {_owner()}
        r2 = await poll_once(bot, state, NOW + 5 * 60_000 + 1)
        assert r2["sent"] == 0, "дошло не всем — не помечаем"
        assert bot.count(555) == 1 and bot.count(_owner()) == 0
        # sent_tg=0, буфер цел — событие не потеряно.
        con = _con(s)
        try:
            left = con.execute(
                "SELECT COUNT(*) FROM event WHERE sent_tg=0").fetchone()[0]
        finally:
            con.close()
        assert left == 1
        assert len(state.grouper.buf) == 1
        bot.fail.clear()
        r3 = await poll_once(bot, state, NOW + 5 * 60_000 + 2)
        assert r3["sent"] == 1
        assert bot.count(555) == 1, "получившему — без дубля"
        assert bot.count(_owner()) == 1, "владелец добрал ровно один раз"
        assert state.grouper.buf == []
        con = _con(s)
        try:
            left = con.execute(
                "SELECT COUNT(*) FROM event WHERE sent_tg=0").fetchone()[0]
        finally:
            con.close()
        assert left == 0

    asyncio.run(_go())


def _backlog_ids(texts: list[str]) -> list[str]:
    """Id задач из строк сводок (без заголовка)."""
    out: list[str] = []
    for t in texts:
        for line in t.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                out.append(parts[1].rstrip(":"))
    return out


def test_poll_summary_long_backlog_lossless():
    """MEDIUM: бэклог > 4000 симв. — остаток следующим сообщением, без потерь."""
    async def _go():
        from hub.bot import run as br  # noqa: F401 — _QMSG не трогаем

        s = Store()
        bc.remember_chat(s, 555, NOW)
        n = 120
        # Без stage в payload: строка ~90 симв., бэклог > 4000 — разрез обязан.
        for i in range(n):
            s.add_event(f"H{i:03d}", "ready", {"text": "x" * 90})
        bot, state = FakeBot(), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["sent"] == 0 and len(state.grouper.buf) == n
        now = NOW + 5 * 60_000 + 1
        total = 0
        rounds = 0
        while True:
            r = await poll_once(bot, state, now)
            total += r["sent"]
            now += 5 * 60_000 + 1
            rounds += 1
            con = _con(s)
            try:
                left = con.execute(
                    "SELECT COUNT(*) FROM event WHERE sent_tg=0"
                    ).fetchone()[0]
            finally:
                con.close()
            if left == 0 and not state.grouper.buf:
                break
            assert rounds < 10, "очередь встала"
        assert rounds > 1, "бэклог ушёл больше чем одним сообщением"
        assert total == n, f"помечено {total} из {n}"
        # Каждая строка — ровно один раз каждому чату, без дублей и потерь.
        for chat in (555, _owner()):
            got = _backlog_ids(bot.texts(chat))
            assert sorted(got) == [f"H{i:03d}" for i in range(n)]
        for t in bot.texts():
            assert len(t) <= bc.MSG_LIMIT

    asyncio.run(_go())


def test_poll_summary_frozen_batch_no_dup_on_new_event():
    """MEDIUM: частичный отказ + новое событие — получивший без дублей."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.add_event("H01", "ready", {"stage": "ready"})
        bot, state = FakeBot(fail={555}), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["sent"] == 0 and len(state.grouper.buf) == 1
        r2 = await poll_once(bot, state, NOW + 5 * 60_000 + 1)
        assert r2["sent"] == 0  # 555 упал, владелец получил H01
        assert bot.count(555) == 0 and bot.count(_owner()) == 1
        # Новое событие в буфере — батч заморожен, владелец не дублируется.
        s.add_event("H02", "ready", {"stage": "ready"})
        r3 = await poll_once(bot, state, NOW + 5 * 60_000 + 2)
        assert r3["sent"] == 0
        assert bot.count(_owner()) == 1, "H01 повторно не шлётся"
        bot.fail.clear()
        r4 = await poll_once(bot, state, NOW + 5 * 60_000 + 3)
        assert r4["sent"] == 1  # H01 доставлен всем, батч разморожен
        r5 = await poll_once(bot, state, NOW + 10 * 60_000 + 4)
        assert r5["sent"] == 1  # H02 следующим окном
        for chat in (555, _owner()):
            got = _backlog_ids(bot.texts(chat))
            assert sorted(got) == ["H01", "H02"], f"чат {chat}: {got}"

    asyncio.run(_go())


def test_proxy_session_without_socks():
    """HIGH: сессия с HTTPS_PROXY строится без aiohttp-socks и шлёт через него."""
    import os
    from unittest import mock

    from hub.bot import run as br

    with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:8080"}):
        assert br.get_proxy() == "http://127.0.0.1:8080"
        sess = br.build_session()
    assert isinstance(sess, br.HttpProxySession)
    assert sess.proxy_url == "http://127.0.0.1:8080"
    # Без прокси — напрямую (None), исключений нигде нет.
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("https_proxy", None)
        assert br.build_session() is None

    posted: dict = {}

    class _FakeCM:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return ('{"ok": true, "result": {"id": 1, "is_bot": true,'
                    ' "first_name": "t", "username": "t"}}')

    class _FakeSession:
        closed = False

        def post(self, url, **kwargs):
            posted.update(kwargs)
            posted["url"] = url
            return _FakeCM()

        async def close(self):
            self.closed = True

    async def _go():
        from aiogram import Bot
        from aiogram.methods import GetMe
        from aiogram.types import User

        bot = Bot(token="123:abc", session=sess)
        sess._session = _FakeSession()
        sess._should_reset_connector = False  # сессия уже установлена
        try:
            user = await sess.make_request(bot, GetMe())
        finally:
            sess._session = None
        assert isinstance(user, User)
        assert posted.get("proxy") == "http://127.0.0.1:8080"
        await sess.close()

    asyncio.run(_go())


def test_poll_pulse_hysteresis_prod():
    """LOW: продюсер с гистерезисом — crashed только со второго тика."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.upsert_task(id="HZ", stage="exec r1", worktree="")
        s.link_session("sz", "opencode", "HZ", "executor", 1, "muse")
        bot, state = FakeBot(), BotState()
        t = NOW
        r1 = await poll_once(bot, state, t)
        t += 15_000
        assert r1["added"] == 0  # baseline молчит
        r2 = await poll_once(bot, state, t)
        t += 15_000
        assert r2["added"] == 0, "первый плохой тик — только отложен"
        assert set(state.pulse_pending) == {"HZ"}
        r3 = await poll_once(bot, state, t)
        assert r3["added"] == 1, "подтверждено вторым тиком"
        assert [e["kind"] for e in state.grouper.buf] == ["crashed"]

    asyncio.run(_go())


def test_status_hides_final():
    """LOW: /status как hub status без --all — без merged/dropped."""
    async def _go():
        from hub.bot import run as br

        s = Store()
        s.upsert_task(id="H8", stage="exec r1", worktree="")
        s.upsert_task(id="H9", stage="merged", worktree="")
        text = await asyncio.to_thread(br._sync_status, NOW)
        assert "H8" in text and "H9" not in text

    asyncio.run(_go())


def test_sync_answer_reasons():
    """LOW: несуществующий вопрос — «Вопрос не найден», отвеченный — «Уже отвечен»."""
    async def _go():
        from hub.bot import run as br

        s = Store()
        ok, short, _ = await asyncio.to_thread(br._sync_answer, 987654, "да", NOW)
        assert not ok and short == "Вопрос не найден."
        qid = _add_question(s)
        ok, short, full = await asyncio.to_thread(br._sync_answer, qid, "да", NOW)
        assert ok and short == "✅ Ответ записан" and "Ответ: да" in full
        ok, short, _ = await asyncio.to_thread(br._sync_answer, qid, "нет", NOW)
        assert not ok and short == "Уже отвечен."

    asyncio.run(_go())


def test_qans_edits_all_chats_and_remembers():
    """LOW: ответ кнопкой правит копии во всех чатах; тап запоминает чат."""
    async def _go():
        from hub.bot import run as br

        br._QMSG.clear()
        br._QSENT.clear()
        s = Store()
        qid = _add_question(s)
        bot = FakeBot()
        br._remember_qmsg(555, 11, qid)
        br._remember_qmsg(_owner(), 22, qid)
        ok, text = await br.handle_qans(bot, 555, f"qans:{qid}:0", NOW)
        assert ok and "Ответ: да" in text
        assert 555 in bc.list_chats(s), "тап — тоже первый контакт"
        edited = {(c, m) for c, m, _ in bot.edited}
        assert (555, 11) in edited and (_owner(), 22) in edited
        assert all("Ответ: да" in t for _, _, t in bot.edited)
        # Опоздавший тап из второго чата — «Уже отвечен» + снять клавиатуру.
        ok2, text2 = await br.handle_qans(bot, _owner(), f"qans:{qid}:1", NOW + 1)
        assert not ok2 and text2 == "Уже отвечен."
        dropped = set(bot.markup_dropped)
        assert (555, 11) in dropped and (_owner(), 22) in dropped
        br._QMSG.clear()
        br._QSENT.clear()

    asyncio.run(_go())


def test_confirm_remembers_chat():
    """LOW: подтверждение кнопкой запоминает чат для рассылки."""
    async def _go():
        from hub.bot import run as br

        s = Store()
        s.upsert_task(id="HC", stage="ready")
        text = await br.handle_confirm(777, "confirm:merge:HC:yes", NOW)
        assert text is not None and "передано Claude" in text
        assert 777 in bc.list_chats(s)
        assert await br.handle_confirm(778, "confirm:nope", NOW) is None
        assert 778 in bc.list_chats(s)

    asyncio.run(_go())


def test_handle_reply_edits_all_copies():
    """LOW: ответ реплаем правит копии вопроса во всех чатах."""
    async def _go():
        from hub.bot import run as br

        br._QMSG.clear()
        br._QSENT.clear()
        s = Store()
        qid = _add_question(s, "продлить?", [])
        bot = FakeBot()
        br._remember_qmsg(555, 31, qid)
        br._remember_qmsg(_owner(), 32, qid)
        # Реплай без mapping (перезапуск бота): резолв через fallback-id.
        ok, short = await br.handle_reply(bot, 555, qid, "продлить", 99, NOW)
        assert ok and short == "✅ Ответ записан"
        edited = {(c, m) for c, m, _ in bot.edited}
        assert (555, 31) in edited
        assert (_owner(), 32) in edited
        assert (555, 99) in edited, "реплайнутое сообщение правится тоже"
        br._QMSG.clear()
        br._QSENT.clear()

    asyncio.run(_go())


def test_handle_reply_remembers_chat():
    """LOW: первое действие чата — реплай: чат попадает в рассылку."""
    async def _go():
        from hub.bot import run as br

        br._QMSG.clear()
        br._QSENT.clear()
        s = Store()
        assert 556 not in bc.list_chats(s)
        qid = _add_question(s, "продлить?", [])
        bot = FakeBot()
        ok, _ = await br.handle_reply(bot, 556, qid, "продлить", 41, NOW)
        assert ok
        assert 556 in bc.list_chats(s), "реплай — тоже первый контакт"
        br._QMSG.clear()
        br._QSENT.clear()

    asyncio.run(_go())


def test_poll_roster_partial_per_chat_retry():
    """LOW: ростер per-chat — упавший чат добирает после починки ровно раз."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        bot, state = FakeBot(fail={555}), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["roster"] is False  # baseline
        s.upsert_task(id="HN", stage="exec r1", worktree="")
        r2 = await poll_once(bot, state, NOW + 3600_000 + 1)
        assert r2["roster"] is False, "дошло не всем — метка не движется"
        assert bot.count(_owner()) == 1 and bot.count(555) == 0
        assert any("Сегодня: задачи хаба" in t for t in bot.texts(_owner()))
        bot.fail.clear()
        r3 = await poll_once(bot, state, NOW + 3600_000 + 2)
        assert r3["roster"] is True
        assert bot.count(555) == 1, "недостававший добрал ровно один раз"
        assert bot.count(_owner()) == 1, "получившему — без дубля"
        r4 = await poll_once(bot, state, NOW + 2 * 3600_000 + 3)
        assert r4["roster"] is False and bot.count() == 2

    asyncio.run(_go())


def test_qmsg_qsent_eviction_consistent():
    """LOW: вытеснение _QMSG/_QSENT синхронно — ссылок на мёртвые нет."""
    from hub.bot import run as br

    br._QMSG.clear()
    br._QSENT.clear()
    for i in range(1200):
        br._remember_qmsg(1, 100 + i, i)
    assert len(br._QMSG) <= 1000
    assert len(br._QSENT) <= 1000
    for key, qid in br._QMSG.items():
        assert qid in br._QSENT, f"qid {qid} вытеснен из _QSENT"
        assert key in br._QSENT[qid], f"ключ {key} потерян в _QSENT"
    seen_keys = set()
    for qid, keys in br._QSENT.items():
        for key in keys:
            assert br._QMSG.get(key) == qid, f"мёртвая ссылка {key}"
            seen_keys.add(key)
    assert set(br._QMSG) == seen_keys
    br._QMSG.clear()
    br._QSENT.clear()


def test_reply_edits_question_no_dup():
    """Ответ реплаем: ядро отдаёт короткий ack + полный текст для правки."""
    s = Store()
    con = _con(s)
    try:
        cur = con.execute(
            "INSERT INTO question(task_id, asked_by, text, options_json,"
            " status, answer, answered_via, ts)"
            " VALUES ('T1','claude','продлить?','[\"да\",\"нет\"]',"
            " 'open','','',?)", (NOW,))
        qid = int(cur.lastrowid)
        con.commit()
    finally:
        con.close()
    from hub.bot import run as br

    ok, short, full = asyncio.run(asyncio.to_thread(br._sync_answer, qid, "да", NOW))
    assert ok and short == "✅ Ответ записан" and "Ответ: да" in full
    row = bc.get_question(s, qid)
    assert row["status"] == "answered" and row["answer"] == "да"


def _kb_rows(markup) -> list:
    """Строки инлайн-клавиатуры из reply_markup (None → [])."""
    return list(getattr(markup, "inline_keyboard", None) or [])


def _has_buttons(markup) -> bool:
    return bool(_kb_rows(markup))


def _is_empty_kb(markup) -> bool:
    """Явно переданная пустая клавиатура (поле reply_markup в запросе)."""
    return markup is not None and _kb_rows(markup) == []


def test_question_keeps_buttons_answer_removes_them():
    """Карточка п.4: вопрос уходит с кнопками, ответ — правка без кнопок.

    aiogram не отправляет reply_markup=None, поэтому снятие кнопок —
    только через явно пустую клавиатуру.
    """
    async def _go():
        from hub.bot import run as br

        br._QMSG.clear()
        br._QSENT.clear()
        s = Store()
        bc.remember_chat(s, 555, NOW)
        qid = _add_question(s)
        bot, state = FakeBot(), BotState()
        r = await poll_once(bot, state, NOW)
        assert r["questions"] == 1
        # Рассылка: каждая копия с кнопками.
        assert len(bot.sent_kb) == 2 and all(_has_buttons(kb) for kb in bot.sent_kb)
        assert len(br._qmsg_keys(qid)) == 2
        ok, text = await br.handle_qans(bot, 555, f"qans:{qid}:0", NOW + 1)
        assert ok and "Ответ: да" in text
        # Обе копии отредактированы и обе — с явно пустой клавиатурой.
        assert len(bot.edited) == 2
        assert all(_is_empty_kb(kb) for kb in bot.edited_kb), bot.edited_kb
        # Опоздавший тап: снятие клавиатуры тоже явное.
        ok2, _ = await br.handle_qans(bot, _owner(), f"qans:{qid}:1", NOW + 2)
        assert not ok2
        assert bot.markup_dropped and len(bot.markup_dropped) == 2
        assert all(_is_empty_kb(kb) for kb in bot.markup_dropped_kb)
        br._QMSG.clear()
        br._QSENT.clear()

    asyncio.run(_go())


def test_handle_reply_removes_buttons_in_all_copies():
    """Ответ реплаем: кнопки сняты во всех копиях и в реплайнутом сообщении."""
    async def _go():
        from hub.bot import run as br

        br._QMSG.clear()
        br._QSENT.clear()
        s = Store()
        qid = _add_question(s, "продлить?", ["да", "нет"])
        bot = FakeBot()
        br._remember_qmsg(555, 41, qid)
        br._remember_qmsg(_owner(), 42, qid)
        ok, short = await br.handle_reply(bot, 556, qid, "продлить", 41, NOW)
        assert ok and short == "✅ Ответ записан"
        # Две копии вопроса + реплайнутое сообщение.
        assert len(bot.edited) == 3
        assert all(_is_empty_kb(kb) for kb in bot.edited_kb), bot.edited_kb
        br._QMSG.clear()
        br._QSENT.clear()

    asyncio.run(_go())


def test_show_confirm_result_drops_confirm_buttons():
    """Подтверждение: после тапа Да/Нет уходят, иначе тап можно повторить."""
    from hub.bot import run as br

    class _Msg:
        def __init__(self, fail_edit: bool = False) -> None:
            self.fail_edit = fail_edit
            self.edits: list[tuple[str, object]] = []
            self.answers: list[str] = []

        async def edit_text(self, text, **kw):
            if self.fail_edit:
                raise RuntimeError("сообщение недоступно")
            self.edits.append((str(text), kw.get("reply_markup")))

        async def answer(self, text, **kw):
            self.answers.append(str(text))

    m = _Msg()
    asyncio.run(br.show_confirm_result(m, "✅ stop H01 — передано Claude"))
    assert m.edits and m.edits[0][0].startswith("✅ stop")
    assert _is_empty_kb(m.edits[0][1]), "кнопки Да/Нет сняты явно"
    assert not m.answers
    # Правка упала — новое сообщение (без клавиатуры отправляется как есть).
    m2 = _Msg(fail_edit=True)
    asyncio.run(br.show_confirm_result(m2, "Отменено."))
    assert m2.answers == ["Отменено."] and not m2.edits
