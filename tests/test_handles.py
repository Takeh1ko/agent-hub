"""Ручки оркестратора через CLI: создать → (движок) → сводка/задача/результат → принять; связь; лимиты."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ahub import cli, comms, events, paths, transitions
from ahub.engine import Engine
from ahub.model import Ev, State
from ahub.store import Store
from tests.enginekit import install_fake, make_project, scout_ok


@pytest.fixture
def env(tmp_path, monkeypatch):
    project = make_project(tmp_path)
    (Path(project.root) / ".hub.toml").write_text(
        f'schema_version = 2\nname = "P"\nworktrees = "{tmp_path / "wt"}"\n'
        'allowed_paths = ["core/**", "tests/**", "docs/**"]\n', encoding="utf-8")
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text(f'projects = ["{project.root}"]\n', encoding="utf-8")
    monkeypatch.chdir(project.root)
    store = Store()
    return store, project


def ahub(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out.strip(), out.err.strip()


def test_scout_cycle(env, capsys):
    store, project = env
    install_fake(store, [scout_ok(report="## Суть\nутечка в core/a.py:1\n\n## Подробно\nмного текста\n")])
    rc, out, err = ahub(capsys, "task", "new", "--kind", "scout", "--title", "где утечка", "--model", "fake",
                        "--spec", "посмотри core/")
    assert rc == 0 and out == "T1 в очереди (scout, fake)", err
    assert Engine(store, project, 1, sleep=lambda s: None).run().state is State.DONE

    rc, out, _ = ahub(capsys, "status")
    assert out.startswith("DONE T1 «где утечка» — отчёт готов") and "непрочитано событий 1" in out
    rc, out, _ = ahub(capsys, "status", "T1")
    assert "итог работника: нашёл" in out and "утечка в core/a.py:1" in out and "Подробно" not in out
    assert events.unacked(store) == []  # прочитал задачу — событие подтверждено
    rc, out, _ = ahub(capsys, "result", "T1", "--full")
    assert "## Подробно" in out and '"summary"' in out
    rc, out, _ = ahub(capsys, "accept", "T1")
    assert out == "T1 принята"
    t = store.get_task(1)
    assert t.state is State.ACCEPTED and not Path(t.worktree).exists()
    arch = Path(project.root) / ".agent-hub"
    assert (arch / "tasks" / "T1" / "report.md").exists() and "T1" in (arch / "tasks.md").read_text()
    assert "принята" in (arch / "tasks" / "T1" / "task.md").read_text()


def test_new_errors_one_line(env, capsys):
    rc, out, err = ahub(capsys, "task", "new", "--kind", "code", "--title", "x", "--paths", "bot/**")
    assert rc == 2 and err.startswith("ошибка: задача не создана:") and "«bot/**» вне" in err and "приёмка" in err


def test_key_idempotent(env, capsys):
    store, _ = env
    install_fake(store, [])
    for _ in range(2):
        rc, out, _ = ahub(capsys, "task", "new", "--kind", "scout", "--title", "x", "--model", "fake", "--key", "k1")
    assert rc == 0 and out.startswith("T1 ") and len(store.list_tasks()) == 1


def test_stop_continue_reject(env, capsys):
    store, _ = env
    install_fake(store, [])
    ahub(capsys, "task", "new", "--kind", "scout", "--title", "x", "--model", "fake")
    assert ahub(capsys, "stop", "T1")[1] == "T1: остановлена"
    assert ahub(capsys, "continue", "T1")[1] == "T1 снова в очереди"
    assert ahub(capsys, "reject", "T1", "--reason", "не нужно")[1] == "T1 отклонена"
    assert store.get_task(1).state is State.REJECTED
    rc, _, err = ahub(capsys, "continue", "T1")
    assert rc == 2 and "продолжить можно" in err
    rc, _, err = ahub(capsys, "status", "T99")
    assert rc == 2 and "нет задачи" in err


def test_wait_and_ack(env, capsys):
    store, _ = env
    comms.owner_message(store, "как там оплата?", project="P")
    rc, out, _ = ahub(capsys, "wait", "--timeout", "5s")
    assert rc == 0 and out == "OWNER «как там оплата?»"
    rc, out, _ = ahub(capsys, "wait", "--timeout", "1s")
    assert rc == 3 and out == ""
    assert events.present(store)
    rc, out, _ = ahub(capsys, "inbox")
    assert out.endswith("как там оплата?") and events.unacked(store) == []
    assert ahub(capsys, "inbox")[1] == "новых сообщений нет"


def test_say_ask_answer_alarms(env, capsys):
    store, _ = env
    assert ahub(capsys, "say", "T12 готова, смотрю")[1] == "отправлено владельцу"
    assert comms.outbox(store)[0]["text"] == "T12 готова, смотрю"
    rc, out, _ = ahub(capsys, "ask", "сливать T12?", "--options", "да,нет")
    assert out.startswith("вопрос #1")
    assert "сливать T12? [да, нет]" in ahub(capsys, "questions")[1]
    assert comms.answer(store, 1, "да") and not comms.answer(store, 1, "нет")
    assert events.lines(store, events.unacked(store)) == ["ANSWER #1 «сливать T12?» → да"]
    comms.raise_alarm(store, "opencode недоступен 12 мин", critical=True)
    rc, out, _ = ahub(capsys, "alarms", "--ack")
    assert out == "ALARM! opencode недоступен 12 мин"
    assert ahub(capsys, "alarms")[1] == "тревог нет"


def test_l1_limit(env, capsys):
    store, _ = env
    for i in range(80):
        tid = store.create_task(project="P", kind="scout", title=f"задача номер {i} с длинным названием для проверки")
        for st in (State.PREPARING, State.WORKING, State.DONE):
            transitions.move(store, tid, st, reason="отчёт готов, суть: " + "подробно " * 10)
    rc, out, _ = ahub(capsys, "status")
    assert len(out.encode()) <= 1500 and "ещё" in out


def test_json_status(env, capsys):
    rc, out, _ = ahub(capsys, "--json", "status")
    data = json.loads(out)
    assert data["active"] == [] and data["waiting"] == []
