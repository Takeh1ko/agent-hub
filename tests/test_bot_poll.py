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
        self.edited: list[tuple[int, int, str]] = []
        self.markup_dropped: list[tuple[int, int]] = []
        self._mid = 0

    async def send_message(self, chat, text, **kwargs):
        if int(chat) in self.fail:
            raise RuntimeError(f"сеть упала → {chat}")
        self._mid += 1
        self.sent.append((int(chat), str(text)))
        return SimpleNamespace(message_id=self._mid)

    async def edit_message_text(self, text, chat_id=None,
                                message_id=None, **kwargs):
        self.edited.append((int(chat_id), int(message_id), str(text)))

    async def edit_message_reply_markup(self, chat_id=None,
                                        message_id=None, **kwargs):
        self.markup_dropped.append((int(chat_id), int(message_id)))

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
        assert not [t for t in bot.texts() if "Итого сегодня" in t]
        s.upsert_task(id="HN", stage="exec r1", worktree="")
        r3 = await poll_once(bot, state, NOW + 2 * HOUR + 2)
        assert r3["roster"] is True
        rosters = [t for t in bot.texts() if "Итого сегодня" in t]
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


def test_poll_summary_long_backlog_lossless():
    """MEDIUM: бэклог > 4000 симв. — остаток следующим сообщением, без потерь."""
    async def _go():
        from hub.bot import run as br  # noqa: F401 — _QMSG не трогаем

        s = Store()
        bc.remember_chat(s, 555, NOW)
        n = 120
        for i in range(n):
            s.add_event(f"H{i:03d}", "ready",
                        {"stage": "ready", "text": "x" * 90})
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
        assert total == n, f"помечено {total} из {n}"
        got: set[str] = set()
        for t in bot.texts(_owner()):
            for line in t.splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2:
                    got.add(parts[1].rstrip(":"))
        assert got == {f"H{i:03d}" for i in range(n)}
        for t in bot.texts():
            assert len(t) <= bc.MSG_LIMIT

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
