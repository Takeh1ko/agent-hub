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
