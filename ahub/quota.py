"""Quota-aware scheduling and visibility helpers (architecture §3, §13).

Tracks provider quotas (window quota, e.g. Gemini via agy) and holds or falls back
tasks when quota thresholds are breached or concurrency caps are reached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ahub import providers, reasons, registry
from ahub.config import QuotaConfig
from ahub.i18n import t as _t
from ahub.model import ROLE_FOR_KIND, Kind, Role
from ahub.providers.base import QuotaBucket
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms


@dataclass
class QuotaCheckResult:
    ok: bool
    fallback_model: str = ""
    wait_reason: str = ""
    event_text: str = ""
    breached_group: str = ""
    breached_bucket: QuotaBucket | None = None
    cap: int = 0


def format_bucket_group(group: str, buckets: list[QuotaBucket]) -> str:
    """Format one group's quota line: "Gemini · 5h 29% (resets 17:16) · week 60%"."""
    b_5h = next((b for b in buckets if b.group == group and b.window == "5h"), None)
    b_week = next((b for b in buckets if b.group == group and b.window in ("weekly", "week")), None)

    rem_5h = f"{int(round(b_5h.remaining * 100))}" if b_5h else ""
    reset_5h = fmt_local(b_5h.reset_at) if b_5h else ""
    rem_week = f"{int(round(b_week.remaining * 100))}" if b_week else ""

    if b_5h and b_week:
        return _t("quota.group_line", group=group, rem_5h=rem_5h, reset_5h=reset_5h, rem_week=rem_week)
    if b_5h:
        return _t("quota.group_5h_only", group=group, rem_5h=rem_5h, reset_5h=reset_5h)
    if b_week:
        return _t("quota.group_week_only", group=group, rem_week=rem_week)
    return group


def format_5h_line(bucket: QuotaBucket) -> str:
    """Format the 5h line for headers: "Gemini 5h 29% (resets 17:16)"."""
    pct = f"{int(round(bucket.remaining * 100))}"
    reset = fmt_local(bucket.reset_at)
    return _t("quota.head_line", group=bucket.group, pct=pct, reset=reset)


def get_model_buckets(store: Store, model_alias: str, force: bool = False) -> tuple[str, list[QuotaBucket]]:
    """Provider name and matching quota buckets for a model alias (ALIAS[:EFFORT] accepted)."""
    try:
        entry = registry.get(store, registry.base_alias(model_alias))
    except registry.RegistryError:
        entry = None

    if entry is None:
        if model_alias.lower().startswith("gemini"):
            try:
                prov = providers.get("agy")
                buckets = prov.quota() if not force else (
                    prov.quota(force=True) if hasattr(prov, "quota") else [])
                return "agy", [b for b in buckets if b.models(model_alias)]
            except KeyError:
                return "", []
        return "", []

    try:
        prov = providers.get(entry.provider)
    except KeyError:
        return "", []

    buckets = prov.quota() if not force else (
        prov.quota(force=True) if hasattr(prov, "quota") else [])
    base = registry.base_alias(model_alias)
    matching = [b for b in buckets if b.models(entry.model_id) or b.models(base) or b.models(entry.alias)]
    return entry.provider, matching


def task_models(task: Task) -> tuple[Role, list[str]]:
    """The role and model refs (ALIAS[:EFFORT]) a task uses."""
    from ahub import tasks as _tasks

    if task.kind is Kind.REVIEW:
        models = _tasks.review_refs(task) or ([_tasks.executor_ref(task)] if task.executor else [])
        return Role.REVIEWER, models
    role = ROLE_FOR_KIND.get(task.kind, Role.EXECUTOR)
    models = [_tasks.executor_ref(task)] if task.executor else []
    return role, models


def is_gemini_task(store: Store, task: Task) -> bool:
    """Whether a task uses a model belonging to the Gemini quota group."""
    _role, models = task_models(task)
    for m in models:
        _prov, buckets = get_model_buckets(store, m)
        if any(b.group == "Gemini" for b in buckets):
            return True
        if m.lower().startswith("gemini"):
            return True
    return False


def pick_window(buckets: list[QuotaBucket]) -> QuotaBucket | None:
    """The bucket a quota wait refers to: 5h first, else weekly."""
    return next((b for b in buckets if b.window == "5h"), None) or \
        next((b for b in buckets if b.window in ("weekly", "week")), None)


def describe_error(label: str, buckets: list[QuotaBucket], err: str, fallback: str = "") -> tuple[str, str]:
    """(stored reason, fallback event text) for a turn that failed with a quota error.

    A known window → the wait/fallback reason naming it; no windows (e.g. opencode,
    which reports no quota) → the provider error itself. Event text is "" without a fallback.
    """
    target = pick_window(buckets)
    if target is None:
        reason = reasons.dump("quota", err=err[:300])
        event = _t("engine.quota_fallback_err", label=label, err=err[:300],
                   fallback=fallback) if fallback else ""
        return reason, event
    pct = int(round(target.remaining * 100))
    if fallback:
        event = _t("engine.quota_fallback", label=label, group=target.group,
                   window=target.window, pct=pct, fallback=fallback)
        return reasons.dump("quota_fallback", group=target.group, window=target.window,
                            pct=pct, model=fallback), event
    reset_str = fmt_local(target.reset_at) if target.reset_at else ""
    return reasons.dump("wait_quota", group=target.group, window=target.window,
                        pct=pct, reset=reset_str), ""


def check_quota_for_task(store: Store, task: Task, quota_cfg: QuotaConfig,
                         group_counts: dict[str, int]) -> QuotaCheckResult:
    """Check quota thresholds and concurrency cap for a task before launch."""
    role, models = task_models(task)
    fb_role = quota_cfg.fallback_reviewer if role is Role.REVIEWER else quota_cfg.fallback_executor
    fallback = fb_role or quota_cfg.fallback
    now = now_ms()

    for m in models:
        _prov_name, buckets = get_model_buckets(store, m)
        if not buckets:
            continue
        b_5h = next((b for b in buckets if b.window == "5h"), None)
        b_week = next((b for b in buckets if b.window in ("weekly", "week")), None)

        breached: QuotaBucket | None = None
        if b_5h and b_5h.remaining < quota_cfg.min_5h and now < b_5h.reset_at:
            breached = b_5h
        elif b_week and b_week.remaining < quota_cfg.min_weekly and now < b_week.reset_at:
            breached = b_week

        if breached is not None:
            pct = int(round(breached.remaining * 100))
            if fallback:
                event_text = _t("engine.quota_fallback", label=task.label, group=breached.group,
                                window=breached.window, pct=pct, fallback=fallback)
                return QuotaCheckResult(ok=False, fallback_model=fallback, event_text=event_text,
                                        breached_group=breached.group, breached_bucket=breached)
            reset_str = fmt_local(breached.reset_at)
            reason = reasons.dump("wait_quota", group=breached.group, window=breached.window,
                                  pct=pct, reset=reset_str)
            return QuotaCheckResult(ok=False, wait_reason=reason, breached_group=breached.group,
                                    breached_bucket=breached)

        if b_5h:
            cap = max(1, math.ceil(b_5h.remaining * 6))
            running = group_counts.get(b_5h.group, 0)
            if running >= cap:
                reason = reasons.dump("wait_quota_concurrency", group=b_5h.group, running=running, max=cap)
                return QuotaCheckResult(ok=False, wait_reason=reason, breached_group=b_5h.group, cap=cap)

    return QuotaCheckResult(ok=True)
