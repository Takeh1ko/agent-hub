"""Model providers. A provider module plugs in by name; the core only knows the contract (base.Provider)."""

from __future__ import annotations

import importlib

from ahub.providers.base import Provider

_REGISTRY: dict[str, str] = {
    "opencode": "ahub.providers.opencode:OpencodeProvider",
    "agy": "ahub.providers.agy:AgyProvider",
    "codex": "ahub.providers.codex:CodexProvider",
    "fake": "ahub.providers.fake:FakeProvider",
}
_cache: dict[str, Provider] = {}


def names() -> list[str]:
    return [n for n in _REGISTRY if n != "fake"]


def get(name: str) -> Provider:
    """Provider instance by name (one per process)."""
    if name not in _cache:
        spec = _REGISTRY.get(name)
        if spec is None:
            raise KeyError(f"unknown provider: {name}")
        mod, cls = spec.split(":")
        _cache[name] = getattr(importlib.import_module(mod), cls)()
    return _cache[name]


def register(name: str, provider: Provider) -> None:
    """Swap in / add a provider (tests, external modules)."""
    _cache[name] = provider
