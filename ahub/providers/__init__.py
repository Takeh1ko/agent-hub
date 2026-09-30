"""Поставщики моделей. Модуль поставщика подключается по имени; ядро знает только контракт (base.Provider)."""

from __future__ import annotations

import importlib

from ahub.providers.base import Provider

_REGISTRY: dict[str, str] = {
    "opencode": "ahub.providers.opencode:OpencodeProvider",
    # "agy": "ahub.providers.agy:AgyProvider",  — V29
    "fake": "ahub.providers.fake:FakeProvider",
}
_cache: dict[str, Provider] = {}


def names() -> list[str]:
    return [n for n in _REGISTRY if n != "fake"]


def get(name: str) -> Provider:
    """Экземпляр поставщика по имени (один на процесс)."""
    if name not in _cache:
        spec = _REGISTRY.get(name)
        if spec is None:
            raise KeyError(f"неизвестный поставщик: {name}")
        mod, cls = spec.split(":")
        _cache[name] = getattr(importlib.import_module(mod), cls)()
    return _cache[name]


def register(name: str, provider: Provider) -> None:
    """Подменить/добавить поставщика (тесты, внешние модули)."""
    _cache[name] = provider
