"""Model catalog: one table for every model the hub can use (task T163).

Every registry alias enriched with its provider catalog entry (display name, vendor,
reasoning levels, plan and prices), the quota/Go numbers and the roles where it is
the default. `ahub models`, `ahub setup`'s model step, `ahub providers` and the
console's /models all read through here, so the numbers match everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass

from ahub import registry
from ahub.i18n import t as _t
from ahub.model import Role
from ahub.providers.base import CatalogEntry, PlanKind
from ahub.store import Store

_AGY_LEVELS = ("low", "medium", "high", "xhigh", "max", "ultra")


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
    """Catalog per hub provider; a provider without it gives [] (never raises)."""
    from ahub import providers as _providers

    out: dict[str, list[CatalogEntry]] = {}
    for name in _providers.names():
        try:
            prov = _providers.get(name)
        except KeyError:
            continue
        try:
            if refresh:
                try:
                    out[name] = list(prov.catalog(refresh=True))
                except TypeError:
                    out[name] = list(prov.catalog())
            else:
                out[name] = list(prov.catalog())
        except (OSError, ValueError, RuntimeError):
            out[name] = []
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
    """Alias level + the others available (e.g. "xhigh (low, high)"), "—" when neither."""
    level = alias_level(entry)
    avail = available_levels(entry, info, index)
    others = [r for r in avail if r != level] if level else list(avail)
    if level and others:
        return f"{level} ({', '.join(others)})"
    if level:
        return level
    if others:
        return ", ".join(others)
    return _t("models.no_reasoning")


def price_text(entry: registry.ModelEntry, info: CatalogEntry | None,
               quota_pct: float | None = None,
               go_pct: float | None = None, go_limit: float | None = None) -> str:
    """Price cell: "free" · "quota 31%" · "Go 9% of $60" · "$0.15 / $0.60"."""
    plan = registry.plan_kind(entry, info)
    if plan is PlanKind.FREE:
        return _t("models.price_free")
    if plan is PlanKind.SUBSCRIPTION:
        if quota_pct is not None:
            return _t("models.quota_pct", pct=int(round(quota_pct * 100)))
        return plan_label(plan)
    if plan is PlanKind.GO:
        if go_pct is not None and go_limit is not None:
            return _t("models.go_pct", pct=int(round(go_pct * 100)), limit=f"{go_limit:.0f}")
        if info is not None and info.price_in is not None and info.price_out is not None:
            return _t("models.price_pair", pin=_fmt_price(info.price_in),
                       pout=_fmt_price(info.price_out))
        return plan_label(plan)
    # pay-as-you-go: USD per 1M
    if info is not None and info.price_in is not None and info.price_out is not None:
        return _t("models.price_pair", pin=_fmt_price(info.price_in), pout=_fmt_price(info.price_out))
    if info is not None and (info.price_in is not None or info.price_out is not None):
        pin = _fmt_price(info.price_in) if info.price_in is not None else "—"
        pout = _fmt_price(info.price_out) if info.price_out is not None else "—"
        return _t("models.price_pair", pin=pin, pout=pout)
    return plan_label(plan)


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "0"
    return f"${v:g}"


def context_text(info: CatalogEntry | None) -> str:
    """Context window cell: "1M", "200k", else the raw number; "—" when unknown."""
    if info is None or info.context is None:
        return "—"
    try:
        n = int(info.context)
    except (TypeError, ValueError):
        return "—"
    if n >= 1_000_000 and n % 1_000_000 == 0:
        return f"{n // 1_000_000}M"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000 and n % 1000 == 0:
        return f"{n // 1000}k"
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
    """Roles where the alias is the default, in role order."""
    out: list[str] = []
    for role in Role:
        try:
            default = registry.role_default(store, role)
        except (OSError, ValueError, RuntimeError):
            continue
        if default is not None and default.alias == alias:
            out.append(role.value)
    return out


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


def _quota_pct_for(entry: registry.ModelEntry, store: Store | None = None) -> float | None:
    """Remaining 0..1 of the matching quota window (5h first), None when unknown."""
    from ahub import providers as _providers
    from ahub.quota import get_model_buckets, pick_window

    buckets: list = []
    if store is not None:
        try:
            _prov_name, buckets = get_model_buckets(store, entry.alias)
        except (OSError, ValueError, RuntimeError):
            buckets = []
    if not buckets:
        try:
            prov = _providers.get(entry.provider)
            buckets = [b for b in prov.quota() if b.models(entry.model_id) or b.models(entry.alias)]
        except (KeyError, OSError, ValueError, RuntimeError):
            return None
        if not buckets:
            return None
    target = pick_window(buckets)
    return target.remaining if target is not None else None


def _go_numbers() -> tuple[float | None, float | None]:
    """(month Go spend, limit): None when unknown."""
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
        if limit is None:
            return go, None
        pct = (go / limit) if limit else 0.0
        return pct, limit
    except (OSError, ValueError, RuntimeError):
        return None, None


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
    go_pct, go_limit = _go_numbers()
    rows: list[ModelRow] = []
    for e in entries:
        info = match_entry(e, index)
        plan = registry.plan_kind(e, info)
        quota_pct = _quota_pct_for(e, store) if plan is PlanKind.SUBSCRIPTION and e.provider == "agy" else None
        gpct = go_pct if plan is PlanKind.GO and go_pct is not None and go_limit is not None else None
        glim = go_limit if plan is PlanKind.GO else None
        rows.append(ModelRow(
            entry=e,
            info=info,
            plan=plan,
            reasoning=reasoning_text(e, info, index),
            price=price_text(e, info, quota_pct=quota_pct, go_pct=gpct, go_limit=glim),
            context=context_text(info),
            model=model_text(info, fallback=e.model_id),
            roles=roles_where_default(store, e.alias),
            quota_pct=quota_pct,
            go_pct=gpct,
        ))
    return rows, {"catalogs": catalogs, "index": index}
