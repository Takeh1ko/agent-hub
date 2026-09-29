"""Понятный русский для людей (hub/read/human.py) и поля снимка для него."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hub.read import human as hm
from hub.read import snapshot as snap
from hub.store import Store

NOW = 1_789_000_000_000


# --- модели и роли ---

def test_model_names_like_owner_says():
    assert hm.model_name("muse") == "Spark Go"
    assert hm.model_name("musefree") == "Spark бесплатный"
    assert hm.model_name("muse-spark-1.3-contributor", "opencode-go") == "Spark Go"
    assert hm.model_name("muse-spark-1.3-contributor-free", "opencode") == "Spark бесплатный"
    assert hm.model_name("mimo-v2.6-flash", "opencode-go") == "MiMo Flash"
    assert hm.model_name("mimoflash") == "MiMo Flash"
    assert hm.model_name("deepseek-v4.1-flash") == "DeepSeek"
    assert hm.model_name("gemini") == "Gemini"
    assert hm.model_name("какая-то-новая") == "какая-то-новая"
    assert hm.model_name("") == "?"


def test_team_and_roles():
    assert hm.team("muse", ["muse", "mimoflash"]) == "Spark Go → проверка: Spark Go, MiMo Flash"
    assert hm.team("musefree", []) == "Spark бесплатный"
    assert hm.role_name("executor") == "пишет код"
    assert hm.role_name("reviewer") == "проверяет код"
    assert hm.parse_reviewers('["muse", "mimoflash"]') == ["muse", "mimoflash"]
    assert hm.parse_reviewers("не json") == []


# --- этапы ---

def test_stage_view_rounds():
    v = hm.stage_view("exec r1", 1, 2)
    assert v.now == "Пишет код (круг 1 из 2)" and v.style == "ok"
    assert hm.stage_view("exec r2", 2, 2).now == "Исправляет замечания проверки (круг 2 из 2)"
    assert hm.stage_view("gate r1", 1, 2, reviewers=["muse"]).next == "потом проверка кода: Spark Go"
    last = hm.stage_view("review r2", 2, 2)
    assert last.now == "Проверка кода (круг 2 из 2)"
    assert "решает Claude" in last.next
    assert "исполнитель исправляет" in hm.stage_view("review r1", 1, 2).next
    # Без числа кругов — без «из N».
    assert hm.stage_view("exec r1").now == "Пишет код (круг 1)"


def test_stage_view_final_and_reasons():
    assert hm.stage_view("queued").now == "Ждёт в очереди"
    assert hm.stage_view("ready").style == "done"
    arb = hm.stage_view("arbiter", reason="панель молчит")
    assert arb.now == "Нужно решение Claude: проверяющие не ответили"
    assert arb.style == "attention"
    fail = hm.stage_view("failed", reason="gate-fail: forbidden: x.py")
    assert fail.now == "Ошибка: не прошли тесты или правила: forbidden: x.py"
    assert fail.style == "error"
    assert hm.stage_view("stopped", reason="stop владельца").now == "Остановлена: по команде"
    # Незнакомая причина — как есть, незнакомый этап — не падает.
    assert hm.stage_view("arbiter", reason="что-то новое").now.endswith("что-то новое")
    assert hm.stage_view("новый-этап").now == "новый-этап"


def test_progress_path():
    assert hm.progress("queued").startswith("● очередь → ○ код")
    assert hm.progress("review r2") == "✓ очередь → ✓ код → ✓ тесты → ● проверка → ○ готово → ○ слита"
    assert hm.progress("merged").count("✓") == 6
    assert hm.progress("arbiter") == "" and hm.progress("failed") == ""


def test_health_text():
    assert hm.health_text("🟢") == "работает"
    assert hm.health_text("⚫", "queued") == "ждёт"
    assert hm.health_text("⚫", "exec r1") == "процесса нет"
    assert "зависла" in hm.health_text("🔴")


# --- действия, время, слова ---

def test_activity_text():
    assert hm.activity_text("bash: pytest -q tests/") == "запускает тесты"
    assert hm.activity_text("bash: git commit -m x") == "делает коммит"
    assert hm.activity_text("bash: ls -la") == "команда: ls -la"
    assert hm.activity_text("read hub/x.py") == "читает hub/x.py"
    assert hm.activity_text("edit hub/x.py") == "правит hub/x.py"
    assert hm.activity_text("думает") == "думает"
    assert hm.activity_text("-") == "" and hm.activity_text("") == ""
    assert hm.activity_text("Готово, коммит 1a2b") == "Готово, коммит 1a2b"


def test_time_money_words():
    assert hm.ago(10_000) == "сейчас"
    assert hm.ago(5 * 60_000) == "5 мин"
    assert hm.ago(65 * 60_000) == "1 ч 05 мин"
    assert hm.ago(3 * 24 * 60 * 60_000) == "3 дн"
    assert hm.money(0.256) == "$0.26"
    assert hm.tokens(127_508) == "127 тыс."
    assert hm.plural(1, "запуск", "запуска", "запусков") == "1 запуск"
    assert hm.plural(3, "запуск", "запуска", "запусков") == "3 запуска"
    assert hm.plural(11, "запуск", "запуска", "запусков") == "11 запусков"
    assert hm.plural(22, "запуск", "запуска", "запусков") == "22 запуска"
    assert hm.fit("коротко", 10) == "коротко"
    assert hm.fit("очень длинная строка", 10) == "очень дли…"


# --- карточка ---

CARD = """# H13 — hub: повтор при сбое сервера/сети opencode, новая сессия при смене карточки

**Цель.** Три дефекта живого использования 2026-09-29/30 (docs/tasks/_backlog.md): (1) сбой. Второе
предложение не нужно.

**Прочитать.** что-то
"""


def test_parse_card_title_short_goal():
    info = hm.parse_card(CARD, "H13-hub-resilience")
    assert info.title == "Hub: повтор при сбое сервера/сети opencode, новая сессия при смене карточки"
    assert info.short == "Hub: повтор при сбое сервера/сети opencode"[:37] + "…"
    assert info.goal.startswith("Три дефекта живого использования")
    assert info.goal.endswith("(1) сбой.")
    assert "Второе" not in info.goal
    # Без заголовка и цели — id задачи, пустая цель.
    bare = hm.parse_card("просто текст", "T9")
    assert bare.title == "T9" and bare.goal == ""


def test_short_title_cuts_at_first_clause():
    assert hm.short_title("досье лотов для модели-торговца (Retro Rave топ-40) + закуп софта = 0") \
        == "Досье лотов для модели-торговца"
    # Слишком короткая первая часть — не режем по ней.
    assert hm.short_title("фикс (очень важный и длинный)").startswith("Фикс (очень")


def test_card_info_reads_relative_to_worktree_and_caches(tmp_path):
    wt = tmp_path / "wt"
    (wt / "docs/tasks").mkdir(parents=True)
    card = wt / "docs/tasks/H13.md"
    card.write_text(CARD, encoding="utf-8")
    info = hm.card_info(str(wt), "docs/tasks/H13.md", "H13")
    assert info.title.startswith("Hub: повтор")
    assert hm.card_info(str(wt), "docs/tasks/нет.md", "H13").title == "H13"
    assert hm.card_info("", "docs/tasks/H13.md", "H13").title == "H13"


# --- события ---

def _ev(kind, **payload):
    return {"kind": kind, "payload_json": json.dumps(payload, ensure_ascii=False)}


def test_event_text_plain():
    e = hm.event_text
    assert e(_ev("stage", stage="exec r1"), "muse") == "Spark Go пишет код"
    assert e(_ev("stage", stage="exec r2"), "musefree") == "Spark бесплатный исправляет замечания (круг 2)"
    assert e(_ev("stage", stage="review r1"), "muse", ["muse", "mimoflash"]) \
        == "проверка кода: Spark Go, MiMo Flash (круг 1)"
    assert e(_ev("stage", stage="arbiter", reason="панель молчит")) \
        == "нужно решение Claude: проверяющие не ответили"
    assert e(_ev("stage", stage="merged", sha="abc")) == "слита в рабочую ветку"
    assert e(_ev("stage", stage="queued")) == "поставлена в очередь"
    assert "зависла" in e(_ev("stuck", stage="exec r1"))
    assert e(_ev("owner_message", text="Привет\nкак дела")) == "сообщение владельца: «Привет как дела»"
    assert e(_ev("budget_hard", text="go $0.58/$0.50")).startswith("бюджет исчерпан")
    assert e({"kind": "stage", "payload_json": "битый"}) == "смена этапа"


# --- хранилище и снимок ---

def test_store_stage_marks_and_recent_events():
    s = Store()
    s.add_event("T1", "stage", {"stage": "queued"})
    s.add_event("T1", "stuck", {"stage": "exec r1"})
    last = s.add_event("T1", "stage", {"stage": "arbiter", "reason": "панель молчит"})
    s.add_event("T2", "stage", {"stage": "queued"})
    marks = s.stage_marks()
    assert marks["T1"][1] == "панель молчит" and set(marks) == {"T1", "T2"}
    recent = s.recent_events(2)
    assert [e["id"] for e in recent][0] == last and len(recent) == 2


SCHEMA_OC = """
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
"""


def _oc_db(path: Path, rows: list[tuple]) -> Path:
    con = sqlite3.connect(str(path))
    con.executescript(SCHEMA_OC)
    for sid, directory, cost, created in rows:
        con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "p", directory, "t",
                     json.dumps({"id": "muse-spark-1.3-contributor", "providerID": "opencode-go"}),
                     cost, 0, 0, 0, 0, created, created))
    con.commit()
    con.close()
    return path


def test_snapshot_task_fields_for_people(tmp_path):
    wt = tmp_path / "wt"
    (wt / "docs/tasks").mkdir(parents=True)
    (wt / "docs/tasks/H13.md").write_text(CARD, encoding="utf-8")
    s = Store()
    s.upsert_task(id="H13-x", project="agent-hub", stage="exec r1", round=1,
                  worktree=str(wt), card_path="docs/tasks/H13.md", executor="muse",
                  reviewers_json='["muse", "mimoflash"]', updated_at=NOW)
    from hub.pipeline.common import write_extra

    write_extra(s, "H13-x", 3, "", False)
    s.add_event("H13-x", "stage", {"stage": "exec r1", "round": 1, "reason": "исполнитель"})
    got = {t.id: t for t in snap.build(s, NOW, proc_root=tmp_path / "пусто").tasks}["H13-x"]
    assert got.title.startswith("Hub: повтор") and got.goal.startswith("Три дефекта")
    assert got.executor == "muse" and got.reviewers == ["muse", "mimoflash"]
    assert got.max_rounds == 3 and got.reason == "исполнитель"
    assert got.stage_since_ms > 0


def test_snapshot_month_go_counts_from_first_day(tmp_path):
    day = 24 * 60 * 60_000
    s = Store()
    # 2026-09-15 ~12:00 Екб: с 1-го числа — 14 дней; прошлый месяц — 20 дней назад.
    now = 1_789_455_600_000
    db = _oc_db(tmp_path / "oc.db", [("a", "/x", 1.5, now - 2 * day),
                                     ("b", "/x", 0.5, now - 60_000),
                                     ("c", "/x", 9.0, now - 20 * day)])
    got = snap.build(s, now, opencode_db=db, proc_root=tmp_path / "пусто")
    assert abs(got.month_go - 2.0) < 1e-9
    assert abs(got.all_go - 0.5) < 1e-9
    assert "за месяц Go $2.00 из лимита $60/мес" in got.head_text()
    assert "Gemini" not in got.head_text()  # Gemini выключен — в шапке не шумит


def test_roster_text_plain_words(tmp_path):
    t = snap.TaskSnap(
        id="E4-trader-dossier", project="PlayerUP", stage="review r2", round=2, pulse="🟢",
        cost_go=0.25, cost_usd=0.0, context=0, last_activity="думает",
        sessions=[snap.SessionSnap("s1", "reviewer", "mimo-v2.6-flash", "opencode-go",
                                   NOW - 60_000, "🟢", 0.09, True, 1000, "думает")],
        short="Досье лотов для модели-торговца", executor="muse",
        reviewers=["muse", "mimoflash"], max_rounds=2, stage_since_ms=NOW - 13 * 60_000)
    text = snap.Snapshot(tasks=[t], total_go=0.25, total_usd=0.0, now_ms=NOW).roster_text()
    assert text.splitlines()[0] == "🟢 E4 · Досье лотов для модели-торговца"
    assert "Проверка кода (круг 2 из 2) · 13 мин в этапе · $0.25" in text
    assert "MiMo Flash проверяет код: думает (1 мин)" in text
    assert "Дальше: замечаний нет — готово; есть — решает Claude" in text
