"""i18n шаг 2: каталог EN/RU, выбор языка, вывод CLI в обоих языках."""

from __future__ import annotations

import string

import pytest

from ahub import cli, paths
from ahub.i18n import _reset, lang, set_lang, t


def test_keys_match_and_order():
    from ahub.i18n.en import MESSAGES as en
    from ahub.i18n.ru import MESSAGES as ru

    assert set(en) == set(ru) and len(en) > 50
    assert list(en) == list(ru)


def _fields(tpl: str) -> set[str]:
    out = set()
    for _, field, _, _ in string.Formatter().parse(tpl):
        if field is not None:
            out.add(field.split(".")[0].split("[")[0])
    return out


def test_placeholders_match():
    from ahub.i18n.en import MESSAGES as en
    from ahub.i18n.ru import MESSAGES as ru

    for key in en:
        assert _fields(en[key]) == _fields(ru[key]), key


def test_unknown_key():
    with pytest.raises(KeyError):
        t("no.such.key")


def test_lang_order(monkeypatch, tmp_path):
    from ahub import config

    # AHUB_LANG выше конфига и LANG
    monkeypatch.setenv("AHUB_LANG", "en")
    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text('lang = "ru"\n', encoding="utf-8")
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    _reset()
    assert lang() == "en"
    # конфиг выше LANG
    monkeypatch.delenv("AHUB_LANG")
    _reset()
    assert lang() == "ru"
    assert config.load_hub().lang == "ru"
    # LANG ru → ru
    paths.global_config_path().write_text("", encoding="utf-8")
    _reset()
    assert lang() == "ru"
    # LC_MESSAGES ru → ru
    monkeypatch.delenv("LANG")
    monkeypatch.setenv("LC_MESSAGES", "ru_RU.UTF-8")
    _reset()
    assert lang() == "ru"
    # по умолчанию en
    monkeypatch.delenv("LC_MESSAGES")
    _reset()
    assert lang() == "en"


def test_config_lang_invalid(tmp_path):
    from ahub import config

    paths.global_config_path().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_path().write_text('lang = "de"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError, match="lang"):
        config.load_hub()


def _run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_version_status_errors_both_langs(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for code, no_task, no_proj in (("ru", "нет задачи", "не относится"),
                                   ("en", "no such task", "belongs to no project")):
        monkeypatch.setenv("AHUB_LANG", code)
        _reset()
        assert lang() == code
        rc, out, _ = _run(capsys, "version")
        assert rc == 0 and out.startswith("ahub 3.")
        rc, _, err = _run(capsys, "status", "T99")
        assert rc == 2 and no_task in err
        rc, _, err = _run(capsys, "config")
        assert rc == 2 and no_proj in err
    rc, _, err = _run(capsys, "ack", "xx")
    assert rc == 2 and ("event numbers" in err or "номера событий" in err)


def test_lang_flag_overrides_env(capsys, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AHUB_LANG", "ru")
    _reset()
    rc, _, err = _run(capsys, "--lang", "en", "status", "T99")
    assert rc == 2 and "no such task" in err
    _reset()
    rc, _, err = _run(capsys, "status", "T99")
    assert rc == 2 and "нет задачи" in err


def test_set_lang_bad():
    with pytest.raises(ValueError):
        set_lang("de")


def test_locale_first_nonempty_wins(monkeypatch):
    from ahub import i18n

    monkeypatch.delenv("AHUB_LANG", raising=False)
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    i18n._reset()
    assert i18n.lang() == "en"
    monkeypatch.setenv("LC_ALL", "")
    i18n._reset()
    assert i18n.lang() == "ru"


def test_views_events_en(monkeypatch, tmp_path):
    """Шаг 3: L1/L2 и строка DONE на английском (AHUB_LANG=en)."""
    from ahub import events, transitions, views
    from ahub.model import State
    from ahub.store import Store

    monkeypatch.setenv("AHUB_LANG", "en")
    _reset()
    assert lang() == "en"
    store = Store()
    now = 1_000_000_000_000
    active = store.create_task(project="P", kind="scout", title="find leak", executor="fake", now=now)
    for st in (State.PREPARING, State.WORKING):
        transitions.move(store, active, st, now=now)
    store.update_task(active, phase="studying", round=2, now=now)
    waiting = store.create_task(project="P", kind="code", title="pay button", now=now)
    for st in (State.PREPARING, State.WORKING, State.DONE):
        transitions.move(store, waiting, st, reason="report ready",
                         payload={"summary": "found it", "report_bytes": 2150,
                                  "cost_go": 0.04, "cost_usd": 0.01}, now=now)
    queued = store.create_task(project="P", kind="scout", title="later", now=now)
    assert queued

    l1 = views.status_text(store, now=now)
    assert "studying" in l1 and "round 2" in l1
    assert "queued 1" in l1 and "unread events" in l1
    assert "DONE T2" in l1  # коды не переводятся

    done_ev = [e for e in store.events(task_id=waiting, needs_reaction=True) if e.kind == "done"][0]
    line = events.format_line(done_ev, store.get_task(waiting))
    assert line.startswith("DONE T2 code «pay button» — report 2.1 KB")
    assert "$0.04" in line and "real" in line
    assert "отчёт" not in line and "КБ" not in line

    wt = tmp_path / "wt"
    (wt / ".ahub").mkdir(parents=True)
    (wt / ".ahub" / "report.md").write_text("## Summary\nleak in core/a.py:1\n", encoding="utf-8")
    (wt / ".ahub" / "result.json").write_text('{"summary": "found it"}', encoding="utf-8")
    store.update_task(waiting, worktree=str(wt), now=now)
    l2 = views.task_text(store, store.get_task(waiting), now=now)
    assert "state: done" in l2 and "worker result: found it" in l2
    assert "report" in l2 and "KB — summary:" in l2
    assert "cost:" in l2 and "budget" in l2

    assert views.status_text(store, project="NOPE", now=now).startswith("quiet:")
    import re as _re

    assert not _re.search(r"[а-яА-ЯёЁ]", l1) and not _re.search(r"[а-яА-ЯёЁ]", l2 + line)


def test_words_is_a_real_mapping():
    from ahub import archive

    set_lang("en")
    words = dict(archive.STATE_WORDS)
    assert len(words) == len(archive.STATE_WORDS) > 0
    assert all(isinstance(v, str) and v for v in words.values())
    assert archive.STATE_WORDS.get("no-such-state", "x") == "x"
