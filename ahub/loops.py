"""Busy-loop guard (T166): no task may spin in place silently.

- Loop detector: same reason code N times in a row without progress (no new commit,
  no new review verdict, no round forward) → needs_decision `loop` with evidence.
- Stuck session: K continue turns in a row with no new commit and no tool activity
  → needs_decision `stuck_session` with the last outcome.
- Picks: every engine run counts a re-pick (limits["picks"], limits["pick_ts"]);
  the observer alarms when re-picks per hour exceed [limits] picks_per_hour.
"""

from __future__ import annotations

import re
from pathlib import Path

from ahub import reasons
from ahub.i18n import t as _t
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms

LOOP_CODE = "loop"
STUCK_CODE = "stuck_session"
PICK_WINDOW_MS = 60 * 60_000
PICK_TS_CAP = 30


def limits_of(hub=None) -> tuple[int, int, int]:
    """(loop_settles, stuck_continues, picks_per_hour) from the hub config."""
    try:
        from ahub import config as _config

        cfg = hub if hub is not None else _config.load_hub()
        lim = cfg.limits
        return int(lim.loop_settles or 3), int(lim.stuck_continues or 3), int(lim.picks_per_hour or 6)
    except Exception:
        return 3, 3, 6


def task_head(task: Task) -> str:
    """HEAD of the task copy ('' — no copy yet or unreadable)."""
    wt = task.worktree or ""
    if not wt:
        return ""
    try:
        from ahub import workspace as _ws

        if not Path(wt).is_dir():
            return ""
        return _ws.head(wt) or ""
    except Exception:
        return ""


def verdict_count(task: Task) -> int:
    """Review verdict files in the copy (0 — no copy)."""
    wt = task.worktree or ""
    if not wt:
        return 0
    try:
        base = Path(wt) / ".ahub"
        if not base.is_dir():
            return 0
        return sum(1 for p in base.glob("review_r*_*.json") if p.is_file())
    except OSError:
        return 0


def picks_of(task: Task) -> int:
    """Re-pick rounds of the task (0 — never picked)."""
    try:
        return int(task.limits.get("picks") or 0)
    except (TypeError, ValueError):
        return 0


def loop_of(task: Task) -> dict:
    """Loop tracker as stored ({} — none)."""
    loop = task.limits.get("loop")
    return dict(loop) if isinstance(loop, dict) else {}


def stuck_of(task: Task) -> dict:
    """Continue-streak tracker as stored ({} — none)."""
    stuck = task.limits.get("stuck")
    return dict(stuck) if isinstance(stuck, dict) else {}


def record_pick(store: Store, task_id: int, now: int | None = None) -> int:
    """Count one engine run as a re-pick; returns the total."""
    ts = now if now is not None else now_ms()
    t = store.get_task(task_id)
    if t is None:
        return 0
    lim = dict(t.limits)
    picks = picks_of(t) + 1
    lim["picks"] = picks
    stamps = [int(x) for x in (lim.get("pick_ts") or []) if isinstance(x, int)]
    stamps.append(ts)
    lim["pick_ts"] = stamps[-PICK_TS_CAP:]
    store.update_task(task_id, limits=lim, now=ts)
    return picks


def picks_in_hour(task: Task, now: int | None = None) -> int:
    """Re-picks in the last hour (from pick_ts; 0 — none)."""
    ts = now if now is not None else now_ms()
    stamps = task.limits.get("pick_ts")
    if not isinstance(stamps, list) or not stamps:
        return 0
    cutoff = ts - PICK_WINDOW_MS
    return sum(1 for x in stamps if isinstance(x, int) and x >= cutoff)


def picks_in_hour_from_events(store: Store, task_id: int, now: int | None = None) -> int:
    """Fallback count from STATE events to queued in the last hour."""
    from ahub.model import Ev

    ts = now if now is not None else now_ms()
    cutoff = ts - PICK_WINDOW_MS
    n = 0
    try:
        for e in store.events(task_id=task_id):
            if e.kind != Ev.STATE.value or e.ts < cutoff:
                continue
            if str((e.payload or {}).get("to") or "") == "queued":
                n += 1
    except Exception:
        return 0
    return n


def re_picks_last_hour(store: Store, task: Task, now: int | None = None) -> int:
    """Re-picks of the task in the last hour (pick_ts, else STATE events)."""
    n = picks_in_hour(task, now)
    if n:
        return n
    return picks_in_hour_from_events(store, task.id, now)


def note_settle(store: Store, task: Task, code: str, now: int | None = None,
                head: str | None = None, verdicts: int | None = None) -> dict:
    """Record one QUEUED settle with `code`; returns the updated loop tracker.

    Same code with no progress (same head, same verdicts, same round) grows n,
    anything else restarts at 1. Progress resets the streak at once.
    """
    ts = now if now is not None else now_ms()
    prev = loop_of(task)
    cur_head = head if head is not None else task_head(task)
    cur_verdicts = verdicts if verdicts is not None else verdict_count(task)
    cur_round = int(task.round or 0)
    if prev.get("code") == code and code:
        progressed = (
            str(prev.get("head") or "") != cur_head
            or int(prev.get("verdicts") or 0) != cur_verdicts
            or int(prev.get("round") or 0) != cur_round
        )
        if progressed:
            loop: dict = {"code": code, "n": 1, "first": ts, "head": cur_head,
                          "verdicts": cur_verdicts, "round": cur_round}
        else:
            loop = {"code": code, "n": int(prev.get("n") or 0) + 1, "first": int(prev.get("first") or ts),
                    "head": cur_head, "verdicts": cur_verdicts, "round": cur_round}
    else:
        loop = {"code": code, "n": 1, "first": ts, "head": cur_head,
                "verdicts": cur_verdicts, "round": cur_round}
    lim = dict(task.limits)
    lim["loop"] = loop
    store.update_task(task.id, limits=lim, now=ts)
    return loop


def clear_loop(store: Store, task: Task) -> None:
    """Owner acted (continue/rework/edit/model): the loop episode is over."""
    if "loop" in task.limits or "stuck" in task.limits:
        lim = dict(task.limits)
        lim.pop("loop", None)
        lim.pop("stuck", None)
        store.update_task(task.id, limits=lim)


def loop_reason(code: str, n: int, first_ts: int, now: int | None = None) -> str:
    """Stored reason for a loop: code `loop` with the evidence as params."""
    ts = now if now is not None else now_ms()
    mins = max(1, (ts - int(first_ts or ts)) // 60_000)
    return reasons.dump(LOOP_CODE, what=code or "", n=n, mins=mins)


def stuck_reason(n: int, outcome: str = "", session: str = "") -> str:
    """Stored reason for a stuck session: code `stuck_session` with the last outcome."""
    return reasons.dump(STUCK_CODE, n=n, outcome=(outcome or "")[:200], session=(session or "")[:80])


def note_continue(store: Store, task: Task, head: str, session: str,
                  had_tools: bool, now: int | None = None) -> dict:
    """Record one continue turn; returns the updated streak.

    A new commit or tool activity restarts at 0, silence grows n.
    """
    ts = now if now is not None else now_ms()
    prev = stuck_of(task)
    if had_tools:
        stuck: dict = {"n": 0, "head": head, "session": session or ""}
    elif not prev:
        stuck = {"n": 1, "head": head, "session": session or ""}
    elif prev.get("head") and str(prev.get("head")) != head:
        stuck = {"n": 0, "head": head, "session": session or ""}
    else:
        same_session = not session or not prev.get("session") or prev.get("session") == session
        if same_session:
            stuck = {"n": int(prev.get("n") or 0) + 1, "head": head, "session": session or ""}
        else:
            stuck = {"n": 1, "head": head, "session": session or ""}
    lim = dict(task.limits)
    lim["stuck"] = stuck
    store.update_task(task.id, limits=lim, now=ts)
    return stuck


def clear_stuck_progress(store: Store, task: Task, head: str = "", session: str = "") -> None:
    """A turn with progress: the continue streak restarts."""
    lim = dict(task.limits)
    lim["stuck"] = {"n": 0, "head": head, "session": session or ""}
    store.update_task(task.id, limits=lim)


def hold_suffix(task: Task, now: int | None = None) -> str:
    """' (3×, next check 02:10)' for a held/looping queued task, else ''."""
    ts = now if now is not None else now_ms()
    loop = loop_of(task)
    n = int(loop.get("n") or 0)
    hold = task.limits.get("quota_hold")
    hold = hold if isinstance(hold, dict) else {}
    not_before = int(hold.get("not_before") or 0)
    if n <= 1 and not not_before:
        return ""
    parts: list[str] = []
    if n > 1:
        parts.append(f"{n}×")
    if not_before and not_before > ts:
        parts.append(_t("loops.next_check", when=fmt_local(not_before, now=ts)))
    if not parts:
        return ""
    return " (" + ", ".join(parts) + ")"


def queued_line(task: Task, now: int | None = None) -> str:
    """'queued · <reason> (3×, next check …)' — what a held task is, not its last phase."""
    ts = now if now is not None else now_ms()
    reason = reasons.text(task.state_reason)
    suffix = hold_suffix(task, ts)
    # the stored reason already ends with the reset time; the suffix adds the count/next check
    if suffix and reason:
        return f"{task.state.value} · {reason}{suffix}"
    if reason:
        return f"{task.state.value} · {reason}"
    return task.state.value


def loop_alarm_text(task: Task, n: int) -> str:
    """Observer ALARM text for too many re-picks."""
    return _t("observer.loop_picks", label=task.label, n=n)


_CODE_RE = re.compile(r'"code"\s*:\s*"([^"]+)"')


def reason_code(stored: str) -> str:
    """Reason code of a stored blob ('' — plain text)."""
    try:
        return str(reasons.load(stored).get("code") or "")
    except Exception:
        return ""
