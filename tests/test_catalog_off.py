"""get_catalogs skips a switched-off provider: no shell-out, an empty catalog."""
from __future__ import annotations

from ahub import catalog, providers, registry


def test_switched_off_provider_catalog_not_fetched(monkeypatch):
    class _On:
        name = "opencode"

        def catalog(self, refresh=False):
            return []

    class _Off:
        name = "agy"

        def catalog(self, refresh=False):
            raise AssertionError("a switched-off provider must not be asked for its catalog")

    monkeypatch.setattr(providers, "names", lambda: ["opencode", "agy"])
    monkeypatch.setitem(providers._cache, "opencode", _On())
    monkeypatch.setitem(providers._cache, "agy", _Off())
    monkeypatch.setattr(registry, "disabled_providers", lambda hub=None: frozenset({"agy"}))
    assert catalog.get_catalogs(refresh=True) == {"opencode": [], "agy": []}
