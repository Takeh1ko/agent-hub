"""Каталог строк EN/RU: t(key, **kw) — шаблон текущего языка через str.format.

Нет ключа в языке — шаблон из en; нет нигде — KeyError. Язык — лениво, один раз:
AHUB_LANG → lang в конфиге хаба → LANG/LC_ALL/LC_MESSAGES (ru*) → en.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping

from ahub.i18n.en import MESSAGES as _EN
from ahub.i18n.ru import MESSAGES as _RU

_CATALOGS: dict[str, dict[str, str]] = {"en": _EN, "ru": _RU}

_lang: str | None = None
_resolving: bool = False


def _locale_lang() -> str:
    locale = next((v for v in (os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")) if v), "")
    return "ru" if locale.lower().startswith("ru") else "en"


def _resolve() -> str:
    """AHUB_LANG → lang в конфиге хаба → локаль (первая непустая из LC_ALL, LC_MESSAGES, LANG) → en."""
    global _resolving
    env = os.environ.get("AHUB_LANG", "").strip().lower()[:2]
    if env in _CATALOGS:
        return env
    if _resolving:  # ошибка конфига хаба сама строится через t() — читаем только окружение/локаль
        return _locale_lang()
    _resolving = True
    try:
        try:
            from ahub import config

            configured = config.load_hub().lang
        except config.ConfigError:  # битый конфиг сообщит о себе сам — язык ему для этого и нужен
            configured = ""
    finally:
        _resolving = False
    if configured:
        return configured
    return _locale_lang()


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


class Words(Mapping[str, str]):
    """Слова каталога по ключам (состояния, фазы): значение — t(prefix + key) при чтении, на текущем языке."""

    def __init__(self, prefix: str, keys: Iterable[str]):
        self._prefix = prefix
        self._keys = tuple(keys)

    def __getitem__(self, key: str) -> str:
        if key not in self._keys:
            raise KeyError(key)
        return t(self._prefix + key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)
