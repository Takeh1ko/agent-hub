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
    """Порядок выбора языка (первый подходящий)."""
    v = os.environ.get("AHUB_LANG", "").strip().lower()
    if v in ("en", "ru"):
        return v
    if v.startswith("ru"):
        return "ru"
    if v.startswith("en"):
        return "en"
    if v:
        pass  # неизвестное значение — дальше по порядку
    try:
        from ahub import config as _cfg

        hub = _cfg.load_hub()
    except Exception as e:
        from ahub.config import ConfigError as _CE

        if isinstance(e, _CE) and any("lang" in x for x in e.errors):
            raise
        hub = None
    if hub is not None and getattr(hub, "lang", ""):
        return hub.lang
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        loc = os.environ.get(var, "").strip().lower()
        if loc.startswith("ru"):
            return "ru"
    return "en"


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
    if c not in ("en", "ru"):
        raise ValueError(f"lang: допустимо 'en' или 'ru', получено {code!r}")
    _lang = c


def _reset() -> None:
    """Сбросить выбор языка (для тестов)."""
    global _lang
    _lang = None


def t(key: str, **kw) -> str:
    """Шаблон текущего языка, подстановка через str.format(**kw)."""
    cur = lang()
    tpl = _CATALOGS.get(cur, {}).get(key)
    if tpl is None:
        tpl = _EN.get(key)
    if tpl is None:
        raise KeyError(key)
    return tpl.format(**kw)
