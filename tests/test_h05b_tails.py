"""H05b хвосты: боевая БД, мёртвые чаты, разрез сводки, hub status без 🔴."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

from hub.bot import core as bc
from hub.bot.run import BotState, outbox_once, poll_once
from hub.store import Store

NOW = 1_789_000_000_000


def _con(store: Store):
    con = sqlite3.connect(str(store.path))
    con.row_factory = sqlite3.Row
    return con


def _owner() -> int:
    from hub.tg_send import OWNER_CHAT_ID

    return int(OWNER_CHAT_ID)


# --- 1. HIGH: боевая БД не тронута ---

def test_store_uses_temp_home(tmp_path):
    """Store() без пути — внутри подменённого HOME, не в боевой."""
    s = Store()
    assert str(s.path).startswith(str(tmp_path)), f"путь вне tmp: {s.path}"
    assert ".local/share/agent-hub/hub.db" in str(s.path)


def test_no_combat_write(tmp_path):
    """Страж: операции как в утечке (чат 555) не меняют боевой файл."""
    import os
    import pwd

    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    combat = real_home / ".local/share/agent-hub/hub.db"
    before_mtime = combat.stat().st_mtime if combat.exists() else None
    before_size = combat.stat().st_size if combat.exists() else None
    s = Store()
    assert str(s.path).startswith(str(tmp_path))
    s.upsert_task(id="GUARD", stage="exec r1", worktree="")
    bc.remember_chat(s, 555, NOW)
    assert 555 in bc.list_chats(s)
    if before_mtime is None:
        assert not combat.exists(), "боевой файл создан тестами"
    else:
        assert combat.exists()
        assert combat.stat().st_mtime == before_mtime, "боевой файл изменён"
        assert combat.stat().st_size == before_size


def test_status_resolves_home_at_call(tmp_path, monkeypatch):
    """Путь чужой БД берётся при вызове, константы импорта нет."""
    import hub.commands.status as st

    assert not hasattr(st, "DEFAULT_OPENCODB"), "утечка HOME при импорте"
    home2 = tmp_path / "home-после-импорта"
    db = home2 / ".local/share/opencode/opencode.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"")
    monkeypatch.setenv("HOME", str(home2))
    assert st.default_opencode_db() == db


def test_default_path_respects_env_at_call(tmp_path, monkeypatch):
    """AGENT_HUB_HOME после импорта меняет Store().path."""
    from hub.store import default_path

    home2 = tmp_path / "home2"
    monkeypatch.setenv("AGENT_HUB_HOME", str(home2 / "hub-home"))
    assert str(default_path()).startswith(str(home2))


# --- 2. HIGH: мёртвые чаты ---

def test_is_dead_chat_error():
    assert bc.is_dead_chat_error(RuntimeError("chat not found"))
    assert bc.is_dead_chat_error(RuntimeError("Bad Request: chat not found"))
    assert bc.is_dead_chat_error(RuntimeError("bot was blocked by the user"))
    assert bc.is_dead_chat_error(RuntimeError("Forbidden: bot was blocked"))
    assert bc.is_dead_chat_error(RuntimeError("FORBIDDEN"))
    assert bc.is_dead_chat_error(
        RuntimeError("Bad Request: group chat was deleted"))
    assert bc.is_dead_chat_error(RuntimeError("chat was deleted"))
    assert bc.is_dead_chat_error(RuntimeError("user is deactivated"))
    assert bc.is_dead_chat_error(RuntimeError("Bad Request: PEER_ID_INVALID"))
    assert bc.is_dead_chat_error(RuntimeError("Bad Request: user not found"))
    assert bc.is_dead_chat_error(RuntimeError("Bad Request: chat_id is empty"))
    assert not bc.is_dead_chat_error(RuntimeError("сеть упала"))
    assert not bc.is_dead_chat_error(RuntimeError("timeout"))
    assert not bc.is_dead_chat_error(RuntimeError(""))


def test_forget_chat_removes():
    s = Store()
    bc.remember_chat(s, 777, NOW)
    assert 777 in bc.list_chats(s)
    bc.forget_chat(s, 777)
    assert 777 not in bc.list_chats(s)
    bc.forget_chat(s, 777)  # повтор — без ошибки


class DeadBot:
    """Фейк: один чат мёртв (chat not found), остальные живы."""

    def __init__(self, dead: set[int], dead_text: str = "Bad Request: chat not found") -> None:
        self.dead = set(dead)
        self.dead_text = str(dead_text)
        self.sent: list[tuple[int, str]] = []
        self._mid = 0

    async def send_message(self, chat, text, **kwargs):
        if int(chat) in self.dead:
            raise RuntimeError(self.dead_text)
        self._mid += 1
        self.sent.append((int(chat), str(text)))
        return SimpleNamespace(message_id=self._mid)

    def count(self, chat: int | None = None) -> int:
        if chat is None:
            return len(self.sent)
        return sum(1 for c, _ in self.sent if c == int(chat))


def test_outbox_dead_chat_removed_no_block():
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        dead = 999999
        bc.remember_chat(s, dead, NOW)
        con = _con(s)
        try:
            con.execute(
                "INSERT INTO outbox(ts, text, task_id, sent_ts)"
                " VALUES (?, 'важно', '', NULL)", (NOW,))
            con.commit()
        finally:
            con.close()
        bot, state = DeadBot(dead={dead}), BotState()
        n = await outbox_once(bot, state, NOW)
        assert n == 1, "мёртвый не держит живых"
        assert bot.count(555) == 1 and bot.count(_owner()) == 1
        assert bot.count(dead) == 0
        assert dead not in bc.list_chats(s), "мёртвый удалён из tg_chat"
        assert 555 in bc.list_chats(s)
        # Повтор — тишина, бесконечного ретрая каждые 5 с нет.
        assert await outbox_once(bot, state, NOW + 5000) == 0
        assert bot.count() == 2

    asyncio.run(_go())


def test_outbox_group_deleted_removed():
    """Удалённая группа: Bad Request: group chat was deleted — удаление, n==1."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        dead = 666666
        bc.remember_chat(s, dead, NOW)
        con = _con(s)
        try:
            con.execute(
                "INSERT INTO outbox(ts, text, task_id, sent_ts)"
                " VALUES (?, 'важно', '', NULL)", (NOW,))
            con.commit()
        finally:
            con.close()
        bot, state = DeadBot(dead={dead},
                             dead_text="Bad Request: group chat was deleted"), BotState()
        n = await outbox_once(bot, state, NOW)
        assert n == 1
        assert bot.count(dead) == 0
        assert dead not in bc.list_chats(s)
        assert dead not in bc.all_chats(s)

    asyncio.run(_go())


def test_dead_owner_excluded_until_write():
    """Мёртвый владелец исключается из рассылки, возврат — после сообщения."""
    s = Store()
    owner = _owner()
    assert owner in bc.all_chats(s)
    bc.forget_chat(s, owner)
    assert owner not in bc.all_chats(s), "мёртвый владелец не в рассылке"
    bc.remember_chat(s, owner, NOW)
    assert owner in bc.all_chats(s), "написавший снова в рассылке"


def test_summary_dead_chat_removed_no_block():
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        dead = 888888
        bc.remember_chat(s, dead, NOW)
        s.add_event("H01", "ready", {"stage": "ready"})
        bot, state = DeadBot(dead={dead}), BotState()
        r1 = await poll_once(bot, state, NOW)
        assert r1["sent"] == 0 and len(state.grouper.buf) == 1
        r2 = await poll_once(bot, state, NOW + 5 * 60_000 + 1)
        assert r2["sent"] == 1, "мёртвый не блокирует пометку"
        assert dead not in bc.list_chats(s)
        # Живые получили по одному, мёртвый — ноль.
        alive_texts = [t for c, t in bot.sent if c != dead]
        assert len(alive_texts) == 2
        assert all("H01" in t for t in alive_texts)

    asyncio.run(_go())


def test_questions_dead_chat_removed():
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        dead = 777777
        bc.remember_chat(s, dead, NOW)
        con = _con(s)
        try:
            con.execute(
                "INSERT INTO question(task_id, asked_by, text, options_json,"
                " status, answer, answered_via, ts)"
                " VALUES ('T1','claude','идём?','[\"да\"]','open','','',?)",
                (NOW,))
            con.commit()
        finally:
            con.close()
        bot, state = DeadBot(dead={dead}), BotState()
        r = await poll_once(bot, state, NOW)
        assert r["questions"] == 1
        assert dead not in bc.list_chats(s)
        assert bot.count(dead) == 0
        assert bot.count(555) == 1

    asyncio.run(_go())


# --- 3. MEDIUM: разрез сводки реальный ---

def test_summary_cut_real():
    """Строки ≥90 симв., 120 событий >4000, rounds>1, каждый ровно раз."""
    evs = [{"id": i + 1, "kind": "ready", "task_id": f"H{i:03d}",
            "payload_json": '{"text": "' + "x" * 90 + '"}'}
           for i in range(120)]
    # Каждая строка — не меньше 90 символов смысла.
    for e in evs:
        assert len(bc.format_event_line(e)) >= 90
    # Весь бэклог — больше лимита (format_grouped режет до лимита).
    assert len(bc.format_grouped(evs)) == bc.MSG_LIMIT
    raw = "Сводка (120):\n" + "\n".join(bc.format_event_line(e) for e in evs)
    assert len(raw) > 4000
    rest = list(evs)
    seen: list[int] = []
    rounds = 0
    while rest:
        text, batch = bc.select_summary_batch(rest)
        assert text and batch
        assert len(text) <= bc.MSG_LIMIT
        # Батч — префикс остатка, без пропусков и перестановок.
        assert batch == [int(e["id"]) for e in rest[:len(batch)]]
        seen.extend(batch)
        rest = rest[len(batch):]
        rounds += 1
        assert rounds < 10
    assert rounds > 1, "разрез обязан: одним сообщением не ушло"
    assert sorted(seen) == list(range(1, 121))
    assert len(set(seen)) == 120, "каждое событие ровно раз"
    cnt = Counter(seen)
    assert all(v == 1 for v in cnt.values())


def test_poll_backlog_each_once():
    """Прод-путь: 120 событий каждому чату ровно раз, без дублей/потерь."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        n = 120
        for i in range(n):
            s.add_event(f"H{i:03d}", "ready", {"text": "x" * 90})

        class CountBot:
            def __init__(self) -> None:
                self.sent: list[tuple[int, str]] = []
                self._mid = 0

            async def send_message(self, chat, text, **kw):
                self._mid += 1
                self.sent.append((int(chat), str(text)))
                return SimpleNamespace(message_id=self._mid)

            def texts(self, chat: int | None = None) -> list[str]:
                if chat is None:
                    return [t for _, t in self.sent]
                return [t for c, t in self.sent if c == int(chat)]

        cb = CountBot()
        state = BotState()
        r1 = await poll_once(cb, state, NOW)
        assert r1["sent"] == 0
        now = NOW + 5 * 60_000 + 1
        total = 0
        rounds = 0
        while True:
            r = await poll_once(cb, state, now)
            total += r["sent"]
            now += 5 * 60_000 + 1
            rounds += 1
            con = _con(s)
            try:
                left = con.execute(
                    "SELECT COUNT(*) FROM event WHERE sent_tg=0").fetchone()[0]
            finally:
                con.close()
            if left == 0 and not state.grouper.buf:
                break
            assert rounds < 10
        assert rounds > 1
        assert total == n
        for chat in (555, _owner()):
            got: list[str] = []
            for t in cb.texts(chat):
                for line in t.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2:
                        got.append(parts[1].rstrip(":"))
            assert sorted(got) == [f"H{i:03d}" for i in range(n)]
            assert len(got) == len(set(got)) == n
        for t in cb.texts():
            assert len(t) <= bc.MSG_LIMIT

    asyncio.run(_go())


# --- 4. MEDIUM: заморозка батча ---

def test_frozen_batch_no_dup_documented():
    """Частичный отказ + новое событие — получивший без дублей (документ)."""
    async def _go():
        s = Store()
        bc.remember_chat(s, 555, NOW)
        s.add_event("H01", "ready", {"stage": "ready"})

        class Flaky:
            def __init__(self) -> None:
                self.fail: set[int] = {555}
                self.sent: list[tuple[int, str]] = []
                self._mid = 0

            async def send_message(self, chat, text, **kw):
                if int(chat) in self.fail:
                    raise RuntimeError("сеть упала")
                self._mid += 1
                self.sent.append((int(chat), str(text)))
                return SimpleNamespace(message_id=self._mid)

            def count(self, chat: int) -> int:
                return sum(1 for c, _ in self.sent if c == int(chat))

            def texts(self, chat: int) -> list[str]:
                return [t for c, t in self.sent if c == int(chat)]

        bot, state = Flaky(), BotState()
        await poll_once(bot, state, NOW)
        await poll_once(bot, state, NOW + 5 * 60_000 + 1)
        assert bot.count(555) == 0 and bot.count(_owner()) == 1
        s.add_event("H02", "ready", {"stage": "ready"})
        await poll_once(bot, state, NOW + 5 * 60_000 + 2)
        assert bot.count(_owner()) == 1, "H01 повторно не шлётся"
        bot.fail.clear()
        await poll_once(bot, state, NOW + 5 * 60_000 + 3)
        await poll_once(bot, state, NOW + 10 * 60_000 + 4)
        for chat in (555, _owner()):
            got = []
            for t in bot.texts(chat):
                for line in t.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2:
                        got.append(parts[1].rstrip(":"))
            assert sorted(got) == ["H01", "H02"]

    asyncio.run(_go())


# --- 5. Бэклог hub status ---

def test_final_pulse_icons_no_red():
    """Финалы — свои значки, без 🔴 даже без процесса и со старым пульсом."""
    from hub.read import snapshot as snap

    for stage, want in [("ready", "✅"), ("merged", "✅"), ("dropped", "✅"),
                        ("arbiter", "⚖️"), ("failed", "❌"), ("stopped", "⏹")]:
        got = snap._pulse_mark(stage, 10 * 3600_000, False, [], False)
        assert got == want, f"{stage}: {got} != {want}"
        assert got != "🔴"
    # Очередь/предполёт без процесса — ⚫, не 🔴.
    assert snap._pulse_mark("queued", 10 * 3600_000, False, [], False) == "⚫"
    assert snap._pulse_mark("preflight", 10 * 3600_000, False, [], False) == "⚫"


def test_final_pulse_icons_build_level(tmp_path):
    """Build-уровень: финалы со старым пульсом без процесса — свои значки, без 🔴."""
    from hub.read import snapshot as snap

    for stage, want in [("ready", "✅"), ("merged", "✅"), ("dropped", "✅"),
                        ("arbiter", "⚖️"), ("failed", "❌"), ("stopped", "⏹")]:
        s = Store()
        tid = f"F-{stage}"
        s.upsert_task(id=tid, stage=stage, worktree="",
                      updated_at=NOW - 10 * 3600_000)
        got = snap.build(s, NOW, opencode_db=None,
                         proc_root=tmp_path / "пустой-proc")
        task = {t.id: t for t in got.tasks}[tid]
        assert task.pulse == want, f"{stage}: {task.pulse} != {want}"
    # Живой exec со старым пульсом без объяснения — по-прежнему 🔴.
    s = Store()
    s.upsert_task(id="FE", stage="exec r1", worktree="", updated_at=NOW - 3600_000)
    got = snap.build(s, NOW, opencode_db=None, proc_root=tmp_path / "пустой2")
    assert {t.id: t for t in got.tasks}["FE"].pulse == "🔴"


def test_task_pulse_all_sessions(tmp_path):
    """Пульс задачи — по всем сессиям: свежий ревьюер перекрывает старого исполнителя."""
    import sqlite3

    from hub.read import snapshot as snap

    s = Store()
    wt = tmp_path / "wt-all"
    wt.mkdir()
    s.upsert_task(id="TA", stage="exec r1", worktree=str(wt), updated_at=NOW)
    s.link_session("exec-old", "opencode", "TA", "executor", 1, "muse")
    s.link_session("rev-fresh", "opencode", "TA", "reviewer", 1, "mimo")
    db = tmp_path / "oc.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
    CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
      title TEXT NOT NULL, model TEXT, cost REAL DEFAULT 0 NOT NULL,
      tokens_input INTEGER DEFAULT 0 NOT NULL, tokens_output INTEGER DEFAULT 0 NOT NULL,
      tokens_cache_read INTEGER DEFAULT 0 NOT NULL, tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
    CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
      priority TEXT NOT NULL, position INTEGER NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
    """)
    for sid, pulse in [("exec-old", NOW - 30 * 60_000), ("rev-fresh", NOW - 30_000)]:
        con.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, "p", str(wt), "t", json.dumps({"id": "muse", "providerID": "opencode-go"}),
             0.1, 0, 0, 0, 0, pulse, pulse))
    con.commit()
    con.close()
    root = tmp_path / "proc"
    rdir = root / "10"
    rdir.mkdir(parents=True)
    (rdir / "cmdline").write_bytes(b"opencode\x00run\x00")
    (rdir / "cwd").symlink_to(wt)
    (rdir / "stat").write_text(
        "10 (x) R 1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 0\n", encoding="utf-8")
    got = snap.build(s, NOW, opencode_db=str(db), proc_root=root)
    task = {t.id: t for t in got.tasks}["TA"]
    # Свежий ревьюер (30 с) перекрывает старого исполнителя (30 мин) → 🟢.
    assert task.pulse == "🟢", f"пульс {task.pulse}, ждали 🟢"
    roles = {ss.role for ss in task.sessions}
    assert {"executor", "reviewer"} <= roles


def test_import_legacy_missing_worktree(tmp_path):
    """Снятый worktree из известного каталога → merged/dropped; чужой/queued целы."""
    s = Store()
    wt_root = tmp_path / "wt-root"
    wt_root.mkdir()
    gone_ready = wt_root / "gone-ready"
    gone_exec = wt_root / "gone-exec"
    s.upsert_task(id="GR", stage="ready", worktree=str(gone_ready))
    s.upsert_task(id="GE", stage="exec r1", worktree=str(gone_exec))
    keep = wt_root / "keep"
    keep.mkdir()
    s.upsert_task(id="KK", stage="exec r1", worktree=str(keep))
    s.upsert_task(id="NW", stage="exec r1", worktree="")
    s.upsert_task(id="QQ", stage="queued", worktree=str(wt_root / "not-yet"))
    outside = tmp_path / "outside-gone"
    s.upsert_task(id="OUT", stage="exec r1", worktree=str(outside))
    ids = s.import_legacy(wt_root)
    assert ids == []
    assert s.get_task("GR")["stage"] == "merged"
    assert s.get_task("GE")["stage"] == "dropped"
    assert s.get_task("KK")["stage"] == "exec r1"
    assert s.get_task("NW")["stage"] == "exec r1"
    assert s.get_task("QQ")["stage"] == "queued", "queued не сносим"
    assert s.get_task("OUT")["stage"] == "exec r1", "чужой каталог не трогаем"


def test_import_legacy_no_dir_no_sweep(tmp_path):
    """Без известного каталога sweep не бежит."""
    s = Store()
    gone = tmp_path / "gone-nodir"
    s.upsert_task(id="ND", stage="exec r1", worktree=str(gone))
    assert s.import_legacy(tmp_path / "нет-каталога") == []
    assert s.get_task("ND")["stage"] == "exec r1"


def test_import_legacy_relative_cwd(tmp_path, monkeypatch):
    """Относительный worktree сверяется от cwd независимо."""
    wt_root = tmp_path / "wrel"
    wt_root.mkdir(exist_ok=True)
    s = Store()
    monkeypatch.chdir(tmp_path)
    assert Path.cwd() == tmp_path
    s.upsert_task(id="REL", stage="exec r1", worktree="wrel/gone")
    assert s.import_legacy("wrel") == []
    assert s.get_task("REL")["stage"] == "dropped"


def test_status_auto_import_and_hides_dropped(tmp_path, capsys, monkeypatch):
    """hub status сам зовёт import_legacy: снятый worktree прячется."""
    import hub.commands.status as st

    gone = tmp_path / "gone-status"
    s = Store()
    s.upsert_task(id="GS", stage="exec r1", worktree=str(gone), updated_at=NOW)
    monkeypatch.setattr(st, "default_opencode_db",
                        lambda: tmp_path / "нет-oc.db")
    monkeypatch.setattr(st, "_worktrees_dirs", lambda: [tmp_path])
    from hub.cli import main

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "GS" not in out, "снятый worktree должен стать dropped и скрыться"
    assert s.get_task("GS")["stage"] == "dropped"


def test_status_fallback_imports_state_json(tmp_path, capsys, monkeypatch):
    """Fallback на .hub.toml в cwd: state.json импортируется без глобального конфига."""
    import hub.commands.status as st

    proj = tmp_path / "proj"
    wt_root = proj / "wt"
    task_dir = wt_root / "T99-x"
    (task_dir / ".agent").mkdir(parents=True)
    (task_dir / ".agent" / "state.json").write_text(json.dumps({
        "task": "T99-x", "base": "b", "worktree": str(task_dir),
        "executor_session": "s", "reviewer_sessions": [],
        "round": 1, "verdicts": [], "status": "ready"}), encoding="utf-8")
    (proj / ".hub.toml").write_text(
        'schema_version = 1\nname = "t"\nworktrees = "wt"\n', encoding="utf-8")
    monkeypatch.chdir(proj)
    monkeypatch.setattr(st, "default_opencode_db",
                        lambda: tmp_path / "нет-oc.db")
    from hub.cli import main

    assert main(["status", "--all"]) == 0
    out = capsys.readouterr().out
    assert "T99-x" in out, "fallback должен импортировать state.json"
    assert Store().get_task("T99-x")["stage"] == "ready"


def test_bot_status_hides_dropped_worktree(tmp_path, monkeypatch):
    """TG /status тоже делает авто-импорт: снятый worktree скрыт."""
    import asyncio

    import hub.commands.status as st
    from hub.bot import run as br

    gone = tmp_path / "gone-bot"
    s = Store()
    s.upsert_task(id="GB", stage="exec r1", worktree=str(gone), updated_at=NOW)
    monkeypatch.setattr(st, "_worktrees_dirs", lambda: [tmp_path])
    text = asyncio.run(asyncio.to_thread(br._sync_status, NOW))
    assert "GB" not in text
    assert s.get_task("GB")["stage"] == "dropped"


def test_last_activity_no_markdown():
    from hub.read.snapshot import clean_activity

    assert clean_activity("**Готово**, коммит `4d79f3`") == "Готово, коммит 4d79f3"
    assert clean_activity("# Заголовок\nвторая") == "Заголовок"
    assert clean_activity("[текст](http://x)") == "текст"
    assert clean_activity("") == "-"
    assert clean_activity("-") == "-"
    assert clean_activity("fix my_var") == "fix my_var"
    assert clean_activity("bash: pytest -q tests/test_bot.py") == \
        "bash: pytest -q tests/test_bot.py"
    assert clean_activity("1 + 2 * 3") == "1 + 2 * 3"
    long_md = "**" + "x" * 100 + "**"
    got = clean_activity(long_md)
    assert len(got) <= 60 and "**" not in got and "`" not in got


def _oc_schema(con) -> None:
    con.executescript("""
    CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, directory TEXT NOT NULL,
      title TEXT NOT NULL, model TEXT, cost REAL DEFAULT 0 NOT NULL,
      tokens_input INTEGER DEFAULT 0 NOT NULL, tokens_output INTEGER DEFAULT 0 NOT NULL,
      tokens_cache_read INTEGER DEFAULT 0 NOT NULL, tokens_cache_write INTEGER DEFAULT 0 NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
    CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
    CREATE TABLE todo (session_id TEXT NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL,
      priority TEXT NOT NULL, position INTEGER NOT NULL,
      time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL);
    """)


def test_last_activity_build_level(tmp_path):
    """Build-уровень: markdown из opencode чистится, длина ≤60."""
    import sqlite3

    from hub.read import snapshot as snap

    s = Store()
    wt = tmp_path / "wt-md"
    wt.mkdir()
    s.upsert_task(id="MD", stage="exec r1", worktree=str(wt), updated_at=NOW)
    s.link_session("md1", "opencode", "MD", "executor", 1, "muse")
    db = tmp_path / "oc-md.db"
    con = sqlite3.connect(str(db))
    _oc_schema(con)
    con.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("md1", "p", str(wt), "t",
         json.dumps({"id": "muse", "providerID": "opencode-go"}),
         0.1, 0, 0, 0, 0, NOW, NOW))
    md_text = "**Готово**, коммит `abc123` [x](http://x) " + "y" * 100
    con.execute(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        ("p1", "m1", "md1", NOW, NOW, json.dumps({"type": "text", "text": md_text})))
    con.commit()
    con.close()
    empty = tmp_path / "proc-md"
    empty.mkdir()
    got = snap.build(s, NOW, opencode_db=str(db), proc_root=empty)
    by_id = {t.id: t for t in got.tasks}
    assert by_id["MD"].last_activity == by_id["MD"].sessions[0].last_activity
    for val in (by_id["MD"].last_activity,
                by_id["MD"].sessions[0].last_activity):
        assert len(val) <= 60, val
        assert "**" not in val and "`" not in val and "](" not in val
