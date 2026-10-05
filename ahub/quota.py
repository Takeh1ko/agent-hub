"""Quota-aware scheduling and visibility helpers (architecture §3, §13).

Tracks provider quotas (window quota, e.g. Gemini via agy) and holds or falls back
tasks when quota thresholds are breached or concurrency caps are reached.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ahub import providers, reasons, registry
from ahub.config import HubConfig, ProjectConfig, QuotaConfig
from ahub.i18n import t as _t
from ahub.model import ROLE_FOR_KIND, Kind, Role
from ahub.providers.base import QuotaBucket
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms

HOLD_STAGE_REVIEW = "review"  # the hold is about the review: the resume skips the worker turn and the gates
HOLD_STAGE_EXECUTOR = "executor"
HOLD_BACKOFF_MS = (15 * 60_000, 30 * 60_000, 60 * 60_000)  # re-pick in 15 min → 30 min → 1 h
HOLD_NOTIFY_MS = 30 * 60_000  # a hold this long with no fallback tells the owner once


@dataclass
class QuotaCheckResult:
    ok: bool
    fallback_model: str = ""
    wait_reason: str = ""
    event_text: str = ""
    breached_group: str = ""
    breached_bucket: QuotaBucket | None = None
    breached_model: str = ""
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
    return check_quota_for_models(store, models, quota_cfg, group_counts, role=role, label=task.label)


def breached(buckets: list[QuotaBucket], quota_cfg: QuotaConfig, now: int) -> QuotaBucket | None:
    """First breached window (5h before weekly); None — above the thresholds or past the reset."""
    b_5h = next((b for b in buckets if b.window == "5h"), None)
    b_week = next((b for b in buckets if b.window in ("weekly", "week")), None)
    if b_5h is not None and b_5h.remaining < quota_cfg.min_5h and now < b_5h.reset_at:
        return b_5h
    if b_week is not None and b_week.remaining < quota_cfg.min_weekly and now < b_week.reset_at:
        return b_week
    return None


def check_quota_for_models(store: Store, models: list[str], quota_cfg: QuotaConfig,
                           group_counts: dict[str, int], *, role: Role, label: str) -> QuotaCheckResult:
    """Quota thresholds (with fallback) and concurrency cap for explicit model refs."""
    fb_role = quota_cfg.fallback_reviewer if role is Role.REVIEWER else quota_cfg.fallback_executor
    fallback = fb_role or quota_cfg.fallback
    now = now_ms()

    for m in models:
        _prov_name, buckets = get_model_buckets(store, m)
        hit = breached(buckets, quota_cfg, now)
        if hit is not None:
            pct = int(round(hit.remaining * 100))
            if fallback:
                event_text = _t("engine.quota_fallback", label=label, group=hit.group,
                                window=hit.window, pct=pct, fallback=fallback)
                return QuotaCheckResult(ok=False, fallback_model=fallback, event_text=event_text,
                                        breached_group=hit.group, breached_bucket=hit,
                                        breached_model=m)
            reset_str = fmt_local(hit.reset_at)
            reason = reasons.dump("wait_quota", group=hit.group, window=hit.window,
                                  pct=pct, reset=reset_str)
            return QuotaCheckResult(ok=False, wait_reason=reason, breached_group=hit.group,
                                    breached_bucket=hit, breached_model=m)

        b_5h = next((b for b in buckets if b.window == "5h"), None)
        if b_5h:
            cap = max(1, math.ceil(b_5h.remaining * 6))
            running = group_counts.get(b_5h.group, 0)
            if running >= cap:
                reason = reasons.dump("wait_quota_concurrency", group=b_5h.group, running=running, max=cap)
                return QuotaCheckResult(ok=False, wait_reason=reason, breached_group=b_5h.group, cap=cap)

    return QuotaCheckResult(ok=True)


def hold_wake_ms(now: int, reset_ms: int, n: int) -> int:
    """When a held task is re-picked: at the bucket reset, or with back-off when the reset is far."""
    backoff = HOLD_BACKOFF_MS[min(max(n, 1) - 1, len(HOLD_BACKOFF_MS) - 1)]
    if reset_ms and reset_ms > now:
        return min(reset_ms, now + backoff)
    return now + backoff


def active_hold(task: Task, now: int | None = None) -> dict:
    """Quota-hold marker still deferring the task ({} — none or expired)."""
    hold = task.limits.get("quota_hold") or {}
    if not isinstance(hold, dict):
        return {}
    if int(hold.get("not_before") or 0) <= (now if now is not None else now_ms()):
        return {}
    return hold


def expire_hold(store: Store, task: Task) -> None:
    """An explicit model change beats the backoff: wake at the next tick, resume info stays."""
    hold = task.limits.get("quota_hold")
    if isinstance(hold, dict) and hold.get("not_before"):
        lim = dict(task.limits)
        lim["quota_hold"] = {**hold, "not_before": 0}
        store.update_task(task.id, limits=lim)


def clear_hold(store: Store, task: Task) -> None:
    """Drop the quota-hold marker: fresh work ahead, nothing to resume."""
    if isinstance(task.limits.get("quota_hold"), dict):
        lim = dict(task.limits)
        lim.pop("quota_hold", None)
        store.update_task(task.id, limits=lim)


def held_models(store: Store, task: Task, stage: str) -> tuple[Role, list[str]]:
    """Models + role the held stage waits on: the review panel for a review hold of a
    code/routine task, otherwise the task's own models (the executor or a review panel)."""
    from ahub import tasks as _tasks

    if stage == HOLD_STAGE_REVIEW and task.kind in (Kind.CODE, Kind.ROUTINE):
        panel = _tasks.review_refs(task)
        if panel:
            return Role.REVIEWER, panel
    return task_models(task)


def menu_reviewer_fallback(store: Store, project: ProjectConfig | None, quota_cfg: QuotaConfig,
                           exclude: list[str] | tuple[str, ...] = (),
                           hub: HubConfig | None = None) -> str:
    """Next reviewer-menu ref above the thresholds ("" — none qualifies).

    Rule: the role menu holds the models picked for review; when the panel reviewer is quota-held
    with no configured fallback, the first menu entry in menu order that is enabled, whose provider
    is on, that the project does not deny and that is above the quota thresholds takes the turn
    instead of waiting. The panel models themselves are never candidates.
    """
    skip = {registry.base_alias(str(e)) for e in exclude}
    now = now_ms()
    for alias, effort, _default in registry.menu_efforts(store, Role.REVIEWER):
        if registry.base_alias(alias) in skip:
            continue
        try:
            entry = registry.get(store, alias)
        except registry.RegistryError:
            continue
        if not entry.enabled or not registry.provider_enabled(entry.provider, hub):
            continue
        if registry.denied_by(entry, project) is not None:
            continue
        _prov, buckets = get_model_buckets(store, registry.model_ref(alias, effort))
        if breached(buckets, quota_cfg, now) is not None:
            continue
        return registry.model_ref(alias, effort)
    return ""


def swap_executor(store: Store, task: Task, fallback: str) -> str:
    """Point the executor at the fallback (base alias + effort); the next turn starts fresh."""
    from ahub import tasks as _tasks

    old = _tasks.executor_ref(task)
    fb_base = registry.base_alias(fallback)
    fb_stored = registry.stored_effort(fallback)
    store.update_task(task.id, executor=fb_base, effort=fb_stored,
                      limits={**task.limits, "fresh_session": True})
    task.executor = fb_base
    try:
        task.effort = fb_stored
    except (AttributeError, TypeError):
        pass
    return old


def swap_panel_ref(store: Store, task: Task, old_ref: str, fallback: str) -> str:
    """Move the exact failing panel entry to the fallback (its siblings stay)."""
    fb_base = registry.base_alias(fallback)
    fb_stored = registry.stored_effort(fallback)
    rev = dict(task.review)
    models = list(rev.get("models") or [])
    efforts = list(rev.get("efforts") or [])
    new_models, new_efforts = [], []
    for i, x in enumerate(models):
        x_base = registry.base_alias(str(x))
        x_stored = str(efforts[i]) if i < len(efforts) else registry.stored_effort(str(x))
        if registry.model_ref(x_base, x_stored) == old_ref:  # only the failing entry moves
            new_models.append(fb_base)
            new_efforts.append(fb_stored)
        else:
            new_models.append(x_base)
            new_efforts.append(x_stored)
    rev["models"] = new_models
    if any(new_efforts):
        rev["efforts"] = new_efforts
    elif "efforts" in rev:
        rev.pop("efforts", None)
    store.update_task(task.id, review=rev)
    return old_ref
