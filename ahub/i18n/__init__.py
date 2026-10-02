"""Каталог строк EN/RU: t(key, **kw) — шаблон текущего языка через str.format.

Нет ключа в языке — шаблон из en; нет нигде — KeyError. Язык — лениво, один раз:
AHUB_LANG → lang в конфиге хаба → LANG/LC_ALL/LC_MESSAGES (ru*) → en.
"""

from __future__ import annotations

import os

from ahub.i18n.en import MESSAGES as _EN
from ahub.i18n.ru import MESSAGES as _RU

_CATALOGS: dict[str, dict[str, str]] = {"en": _EN, "ru": _RU}

_lang: str | None = None


def _resolve() -> str:
    """AHUB_LANG → lang в конфиге хаба → локаль (первая непустая из LC_ALL, LC_MESSAGES, LANG) → en."""
    env = os.environ.get("AHUB_LANG", "").strip().lower()[:2]
    if env in _CATALOGS:
        return env
    try:
        from ahub import config

        configured = config.load_hub().lang
    except config.ConfigError:  # битый конфиг сообщит о себе сам — язык ему для этого и нужен
        configured = ""
    if configured:
        return configured
    locale = next((v for v in (os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")) if v), "")
    return "ru" if locale.lower().startswith("ru") else "en"


def lang() -> str:
    """Текущий язык (\"en\" | \"ru\")."""
    global _lang
    if _lang is None:
        _lang = _resolve()
    return _lang


def set_lang(code: str) -> None:
    """Жёстко задать язык (флаг --lang)."""
    global _lang
    c = code.strip().lower()
    if c not in _CATALOGS:
        raise ValueError(f"lang: допустимо 'en' или 'ru', получено {code!r}")
    _lang = c


def _reset() -> None:
    """Сбросить выбор языка (для тестов)."""
    global _lang
    _lang = None


def t(key: str, **kw) -> str:
    """Шаблон текущего языка, подстановка через str.format(**kw)."""
    tpl = _CATALOGS[lang()].get(key) or _EN[key]  # нет нигде — KeyError: ошибка разработчика
    return tpl.format(**kw)


class Words(dict):
    """Ленивые слова каталога: вид — dict ради старых мест (.get(k, default), [k], `in`).

    Значение — t(prefix + key) в момент чтения, т.е. на языке хаба. Неизвестный ключ —
    default (get), KeyError ([]) или False (`in`).
    """

    def __init__(self, prefix: str, known: tuple[str, ...] | list[str]):
        super().__init__()
        self._prefix = prefix
        self._known = frozenset(known)

    def _word(self, key: str) -> str:
        try:
            return t(f"{self._prefix}{key}")
        except KeyError:
            return key

    def __getitem__(self, key: str) -> str:
        if key in self._known:
            return self._word(key)
        raise KeyError(key)

    def get(self, key: str, default=None):
        if key in self._known:
            return self._word(key)
        return default

    def __contains__(self, key) -> bool:
        return key in self._known
