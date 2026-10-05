"""Model catalog: one table for every model the hub can use (task T163).

Every registry alias enriched with its provider catalog entry (display name, vendor,
reasoning levels, plan and prices), the quota/Go numbers and the roles where it is
the default. `ahub models`, `ahub setup`'s model step, `ahub providers` and the
console's /models all read through here, so the numbers match everywhere.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ahub import registry
from ahub.i18n import t as _t
from ahub.model import Role
from ahub.providers.base import CatalogEntry, PlanKind
from ahub.store import Store

CATALOG_TTL_S = 60.0  # in-memory cache: provider catalogs shell out (seconds per call),
# and hot paths (`ahub status T12`, task views) read them on every render
_cache: dict[str, tuple[float, dict[str, list[CatalogEntry]]]] = {}


def reset_cache() -> None:
    """Drop the cached provider catalogs (tests isolate here; `--refresh` bypasses anyway)."""
    _cache.clear()

_AGY_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")

# Reasoning strength, weakest first: the table shows the available range in this order.
LEVEL_ORDER = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVEL_ORDER)}


def _level_key(lvl: str) -> tuple[int, str]:
    low = (lvl or "").lower()
    return (_LEVEL_RANK.get(low, len(_LEVEL_RANK)), low)


def plan_label(plan: PlanKind) -> str:
    """Human plan label (i18n): free · go-plan · pay-as-you-go · subscription."""
    key = {
        PlanKind.FREE: "plan.free",
        PlanKind.GO: "plan.go_plan",
        PlanKind.PAYG: "plan.payg",
        PlanKind.SUBSCRIPTION: "plan.subscription",
    }[plan]
    return _t(key)


def get_catalogs(refresh: bool = False) -> dict[str, list[CatalogEntry]]:
    """Catalog per hub provider; a provider without it gives [] (never raises).

    Cached in memory for CATALOG_TTL_S (a fetch shells out to every provider); refresh=True
    always re-fetches and refreshes the cache.
    """
    if not refresh:
        hit = _cache.get("catalogs")
        if hit is not None and time.monotonic() - hit[0] < CATALOG_TTL_S:
            return hit[1]
    from ahub import providers as _providers

    out: dict[str, list[CatalogEntry]] = {}
    for name in _providers.names():
        try:
            prov = _providers.get(name)
        except (KeyError, AttributeError):
            continue
        if not hasattr(prov, "catalog"):
            out[name] = []
            continue
        try:
            if refresh:
                try:
                    out[name] = list(prov.catalog(refresh=True))
                except TypeError:
                    out[name] = list(prov.catalog())
            else:
                try:
                    out[name] = list(prov.catalog())
                except TypeError:
                    out[name] = list(prov.catalog(refresh))
        except (OSError, ValueError, RuntimeError, AttributeError):
            out[name] = []
    _cache["catalogs"] = (time.monotonic(), out)
    return out


def index_by_id(catalogs: dict[str, list[CatalogEntry]]) -> dict[str, CatalogEntry]:
    """Full model id → catalog entry (first wins)."""
    index: dict[str, CatalogEntry] = {}
    for entries in catalogs.values():
        for e in entries:
            index.setdefault(e.model_id, e)
    return index


def match_entry(entry: registry.ModelEntry, index: dict[str, CatalogEntry]) -> CatalogEntry | None:
    """Catalog entry for the alias, by full model id."""
    return index.get(entry.model_id)


def _level_from_id(model_id: str) -> str:
    tail = (model_id or "").lower().rsplit("-", 1)[-1]
    return tail if tail in _AGY_LEVELS else ""


def alias_level(entry: registry.ModelEntry) -> str:
    """The alias's own reasoning level: variant, else the agy-style id suffix."""
    if entry.variant:
        return entry.variant
    return _level_from_id(entry.model_id)


def _agy_base(model_id: str) -> str:
    low = (model_id or "").lower()
    for lvl in _AGY_LEVELS:
        suffix = f"-{lvl}"
        if low.endswith(suffix):
            return (model_id or "")[: -len(suffix)]
    return model_id or ""


def available_levels(entry: registry.ModelEntry, info: CatalogEntry | None,
                     index: dict[str, CatalogEntry] | None = None) -> list[str]:
    """Other reasoning levels available for the alias (may be empty)."""
    if info is not None and info.reasoning:
        if entry.provider == "agy" and len(info.reasoning) <= 1 and index is not None:
            # agy lists one level per model id: siblings with the same base are the choice
            base = _agy_base(entry.model_id).lower()
            levels: list[str] = []
            for cand in index.values():
                if cand.plan is not PlanKind.SUBSCRIPTION:
                    continue
                if _agy_base(cand.model_id).lower() == base:
                    for lvl in cand.reasoning:
                        if lvl not in levels:
                            levels.append(lvl)
            return levels
        return list(info.reasoning)
    if entry.provider == "agy" and index is not None:
        base = _agy_base(entry.model_id).lower()
        levels = []
        for cand in index.values():
            if _agy_base(cand.model_id).lower() == base:
                for lvl in cand.reasoning:
                    if lvl not in levels:
                        levels.append(lvl)
        return levels
    return []


def reasoning_text(entry: registry.ModelEntry, info: CatalogEntry | None,
                   index: dict[str, CatalogEntry] | None = None) -> str:
    """Alias level + compact available range (e.g. "xhigh (minimal–xhigh)"), "—" when neither."""
    level = alias_level(entry)
    avail = sorted(set(available_levels(entry, info, index)), key=_level_key)
    if level:
        if not avail or (len(avail) == 1 and avail[0] == level):
            return level
        lo, hi = avail[0], avail[-1]
        span = lo if lo == hi else f"{lo}–{hi}"
        return f"{level} ({span})"
    if not avail:
        return _t("models.no_reasoning")
    if len(avail) == 1:
        return avail[0]
    return f"{avail[0]}–{avail[-1]}"


def price_text(entry: registry.ModelEntry, info: CatalogEntry | None) -> str:
    """Price cell: "$in / $out per 1M" whenever the catalog has a cost, whatever the plan.

    Plan-level usage lives in the provider group header instead. Without a catalog cost:
    free → "free", subscription → "—" (no money per token), otherwise the plan label.
    """
    plan = registry.plan_kind(entry, info)
    if plan is PlanKind.FREE:
        return _t("models.price_free")
    if info is not None and info.price_in is not None and info.price_out is not None:
        return _t("models.price_pair", pin=_fmt_price(info.price_in),
                   pout=_fmt_price(info.price_out))
    if info is not None and (info.price_in is not None or info.price_out is not None):
        pin = _fmt_price(info.price_in) if info.price_in is not None else _t("models.price_none")
        pout = _fmt_price(info.price_out) if info.price_out is not None else _t("models.price_none")
        return _t("models.price_pair", pin=pin, pout=pout)
    if plan is PlanKind.SUBSCRIPTION:
        return _t("models.price_none")
    return plan_label(plan)


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "$0.00"
    return f"${float(v):.2f}"


def context_text(info: CatalogEntry | None) -> str:
    """Context window cell: "200K", "262K", "1M" (rounded, no decimals); "—" when unknown."""
    if info is None or info.context is None:
        return "—"
    try:
        n = int(info.context)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000:
        return f"{round(n / 1_000_000)}M"
    if n >= 1000:
        return f"{round(n / 1000)}K"
    return str(n)


def model_text(info: CatalogEntry | None, *, with_vendor: bool = True, fallback: str = "—") -> str:
    """Display name + vendor ("DeepSeek V4 Flash (DeepSeek)"), id when unknown."""
    if info is not None and info.display_name:
        if with_vendor and info.vendor:
            return f"{info.display_name} ({info.vendor})"
        return info.display_name
    if info is not None and info.model_id:
        return info.model_id.split("/", 1)[-1]
    return fallback


def roles_where_default(store: Store, alias: str) -> list[str]:
    """Roles where the alias is the default, in role order, effort next to each ("executor:xhigh")."""
    out: list[str] = []
    for role in Role:
        try:
            default = registry.role_default(store, role)
        except (OSError, ValueError, RuntimeError):
            continue
        if default is not None and default.alias == alias:
            try:
                level = alias_level(default)
            except (OSError, ValueError, RuntimeError, AttributeError):
                level = ""
            out.append(f"{role.value}:{level}" if level else role.value)
    return out


def identity_text(entry: registry.ModelEntry, info: CatalogEntry | None = None,
                  plan: PlanKind | None = None) -> str:
    """Canonical dim identity: "<display> · <plan> · <level>" (no level part when there is none)."""
    if info is not None and info.display_name:
        display = info.display_name
    else:
        display = (entry.model_id or entry.alias).split("/", 1)[-1] or entry.alias
    try:
        kind = plan if plan is not None else registry.plan_kind(entry, info)
    except (OSError, ValueError, RuntimeError, AttributeError):
        from ahub.providers.base import PlanKind as _Plan

        kind = _Plan.PAYG
    plan_s = plan_label(kind)
    try:
        level = alias_level(entry)
    except (OSError, ValueError, RuntimeError, AttributeError):
        level = ""
    if level:
        return f"{display} · {plan_s} · {level}"
    return f"{display} · {plan_s}"


@dataclass
class ModelRow:
    entry: registry.ModelEntry
    info: CatalogEntry | None
    plan: PlanKind
    reasoning: str
    price: str
    context: str
    model: str
    roles: list[str]
    quota_pct: float | None = None
    go_pct: float | None = None


def _quota_pct_for(entry, store: Store | None = None) -> float | None:
    """Remaining 0..1 of the matching quota window (5h first), None when unknown."""
    from ahub import providers as _providers
    from ahub.quota import get_model_buckets, pick_window

    alias = getattr(entry, "alias", "") or ""
    provider = getattr(entry, "provider", "") or ""
    model_id = getattr(entry, "model_id", "") or ""
    buckets: list = []
    if store is not None and alias:
        try:
            _prov_name, buckets = get_model_buckets(store, alias)
        except (OSError, ValueError, RuntimeError):
            buckets = []
    if not buckets:
        try:
            prov = _providers.get(provider)
            buckets = [b for b in prov.quota() if b.models(model_id) or b.models(alias)]
        except (KeyError, OSError, ValueError, RuntimeError, AttributeError):
            return None
        if not buckets:
            return None
    target = pick_window(buckets)
    return target.remaining if target is not None else None


def _go_numbers() -> tuple[float | None, float | None]:
    """(month Go spend, limit): (None, None) when unknown, (spend, None) with no limit set."""
    try:
        from ahub import config as _config
        from ahub import cost as _cost
        from ahub.providers import opencode_db as _odb
        from ahub.time import now_ms

        now = now_ms()
        month0 = _cost.month_start(now)
        month = _odb.totals(month0)
        go = month.cost_go or 0.0
        try:
            limit = _config.load_hub().go_month_limit
        except _config.ConfigError:
            limit = None
        return go, limit
    except (OSError, ValueError, RuntimeError):
        return None, None


def go_summary() -> str:
    """Plan-level Go usage for a provider group header: " · Go plan: $5.29 of $60 this month (9%)"."""
    spend, limit = _go_numbers()
    if spend is None or limit is None:
        return ""
    pct = int(round(spend / limit * 100)) if limit else 0
    return " · " + _t("models.go_plan_summary", spend=f"{spend:.2f}", limit=f"{limit:.0f}", pct=pct)


_WINDOW_SHORT = {"5h": "5h", "weekly": "week", "week": "week"}


def quota_summary(provider: str) -> str:
    """Plan-level quota for a provider group header: " · Gemini quota: 5h 87% · week 31%"."""
    from ahub import providers as _providers

    try:
        buckets = _providers.get(provider).quota()
    except (KeyError, OSError, ValueError, RuntimeError, AttributeError):
        return ""
    groups: list[str] = []
    for b in buckets or []:
        if b.group not in groups:
            groups.append(b.group)
    out: list[str] = []
    for group in groups:
        parts = []
        for window in ("5h", "weekly", "week"):
            b = next((x for x in buckets if x.group == group and x.window == window), None)
            if b is not None:
                parts.append(f"{_WINDOW_SHORT[window]} {int(round(b.remaining * 100))}%")
        if parts:
            out.append(_t("models.quota_plan_summary", group=group, parts=" · ".join(parts)))
    return (" · " + " · ".join(out)) if out else ""


def group_title(provider: str) -> str:
    """Provider group header: the name plus plan-level usage (Go spend, quota windows) when known."""
    if provider == "opencode":
        return provider + go_summary()
    return provider + quota_summary(provider)


def provider_order(names: list[str]) -> list[str]:
    """Hub providers in registration order, then any others alphabetically."""
    from ahub import providers as _providers

    known = _providers.names()
    ordered = [n for n in known if n in names]
    return ordered + sorted({n for n in names if n not in ordered})


def visible_entries(entries: list[registry.ModelEntry]) -> list[registry.ModelEntry]:
    """Entries shown in the tables: legacy names hidden, fake only with AHUB_FAKE_PROVIDER=1."""
    from ahub.providers.fake import selectable_from_env

    hidden = set(getattr(registry, "HIDDEN_ALIASES", frozenset()))
    if selectable_from_env():
        return [e for e in entries if e.alias not in hidden]
    return [e for e in entries if e.provider != "fake" and e.alias not in hidden]


def _plan_width() -> int:
    """Plan column cap: the longest plan label, so "pay-as-you-go" is never clipped."""
    try:
        return max(len(plan_label(p)) for p in PlanKind)
    except (OSError, ValueError, RuntimeError, AttributeError):
        return len("pay-as-you-go")


def table_columns(w: int) -> tuple[bool, bool, list[int | None]]:
    """Column toggles and caps for the models table at width w.

    At 140+ nothing is clipped; below that context goes first, then the vendor inside
    the model cell; alias and plan stay whatever the width (plan sized to its longest label).
    """
    with_vendor = w >= 100
    with_context = w >= 120
    plan_w = _plan_width()
    if w >= 140:
        maxw: list[int | None] = [34, None, 24, plan_w, 18]
    else:
        maxw = [32, 28, 16, plan_w, 16]
    if with_context:
        maxw.append(8)
    maxw.append(None)
    return with_vendor, with_context, maxw


def build_rows(store: Store, entries: list[registry.ModelEntry] | None = None,
               refresh: bool = False, index: dict[str, CatalogEntry] | None = None,
               catalogs: dict[str, list[CatalogEntry]] | None = None) -> tuple[list[ModelRow], dict]:
    """Rows for the table + the raw index (for --json and narrow checks)."""
    if entries is None:
        entries = registry.models(store)
    if catalogs is None:
        catalogs = get_catalogs(refresh=refresh)
    if index is None:
        index = index_by_id(catalogs)
    spend, go_limit = _go_numbers()
    rows: list[ModelRow] = []
    for e in entries:
        info = match_entry(e, index)
        try:
            plan = registry.plan_kind(e, info)
        except (OSError, ValueError, RuntimeError, AttributeError):
            plan = PlanKind.PAYG
        provider = getattr(e, "provider", "") or ""
        alias = getattr(e, "alias", "") or ""
        model_id = getattr(e, "model_id", "") or alias
        quota_pct = _quota_pct_for(e, store) if plan is PlanKind.SUBSCRIPTION and provider == "agy" else None
        gpct = (spend / go_limit) if plan is PlanKind.GO and spend is not None and go_limit else None
        try:
            reasoning = reasoning_text(e, info, index)
        except (OSError, ValueError, RuntimeError, AttributeError):
            reasoning = ""
        try:
            price = price_text(e, info)
        except (OSError, ValueError, RuntimeError, AttributeError):
            price = ""
        try:
            roles = roles_where_default(store, alias)
        except (OSError, ValueError, RuntimeError, AttributeError):
            roles = []
        rows.append(ModelRow(
            entry=e,
            info=info,
            plan=plan,
            reasoning=reasoning,
            price=price,
            context=context_text(info),
            model=model_text(info, fallback=model_id),
            roles=roles,
            quota_pct=quota_pct,
            go_pct=gpct,
        ))
    return rows, {"catalogs": catalogs, "index": index}
