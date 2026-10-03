"""What the orchestrator sees: L1 (status), L2 (task detail), history, questions, inbox — the levels and
byte limits of contracts §5. The drawing primitives live in ahub/ui.py, the reason codes in ahub/reasons.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ahub import archive, events, reasons, ui, workspace
from ahub.i18n import Words
from ahub.i18n import t as _t
from ahub.model import ACTIVE, WAITING_DECISION, State
from ahub.store import Store, Task
from ahub.time import now_ms
from ahub.ui import Value

L1_LIMIT = 1500
L1_RESERVE = 120  # room for the "+N more" line and the counters under it
L2_LIMIT = 4000
L3_DEFAULT = 20000
SUMMARY_BYTES = 900  # the byte budget of every block in L2 (the whole L2 stays under 4 KB)
QUESTION_BYTES = 240
NOTES_BYTES = 400
REPORT_BYTES = 700
UNREAD_LINES = 3
HISTORY_STATES = frozenset({State.ACCEPTED, State.REJECTED, State.DONE, State.NEEDS_DECISION, State.ERROR,
                            State.STOPPED})
# a decision is pending — the detail view offers the commands
DECISION_STATES = frozenset({State.DONE, State.NEEDS_DECISION})
RESUME_STATES = frozenset({State.ERROR, State.STOPPED})
PHASE_WORDS: Words = Words("views.phase_", ("studying", "writing", "testing", "waiting"))


def clip_bytes(text: str, limit: int) -> str:
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    cut = b[: limit - 4].decode("utf-8", "ignore")
    return cut.rsplit("\n", 1)[0] + "\n…" if "\n" in cut else cut + "…"


def age(ms: int, now: int | None = None) -> str:
    """How long ago (in minutes) — "4 h 0 min"; under a minute, seconds."""
    now = now if now is not None else now_ms()
    if now - ms < 60_000:
        return _t("views.age_sec", s=max(0, (now - ms) // 1000))
    m = max(0, (now - ms) // 60000)
    if m < 60:
        return _t("views.age_min", m=m)
    if m < 24 * 60:
        return _t("views.age_hm", h=m // 60, m=m % 60)
    return _t("views.age_dh", d=m // 1440, h=(m % 1440) // 60)


def _short(s: str, n: int) -> str:
    """Shorten to n characters — the same word-boundary rule as a table cell (ui.clip)."""
    return ui.clip(s, n)


def state_word(state: State | str) -> str:
    """The state name in the current language."""
    value = state.value if isinstance(state, State) else str(state)
    return archive.STATE_WORDS.get(value, value)


def _cap(head: str, groups: list[tuple[str, list[str]]], tail: str) -> str:
    """The L1 budget (contracts §5): the header, as many task rows of each group as fit, the counters.

    A group that does not fit is cut after its last whole row and the rest is counted, not dropped
    silently: `ahub top` shows every task, and the queued ones also in `ahub service status`.
    """
    out = [head] if head else []
    more = 0
    for title, rows in groups:
        shown, used = [], 0
        for row in rows:
            size = len(row.encode("utf-8")) + 1 + (len(title.encode("utf-8")) + 1 if not shown else 0)
            if used + size > L1_LIMIT - L1_RESERVE - _bytes(out):
                break
            shown.append(row)
            used += size
        more += len(rows) - len(shown)
        if shown:
            out += ([title] if title else []) + shown
    if more:
        out.append(_t("views.more_rows", n=more))
    if tail:
        out.append(tail)
    return clip_bytes("\n".join(out), L1_LIMIT)


def _bytes(lines: list[str]) -> int:
    return sum(len(ln.encode("utf-8")) + 1 for ln in lines)


def _state_cell(t: Task) -> str:
    """The state column: the phase of an active task (what it is doing), else the state."""
    if t.state in ACTIVE and t.phase:
        return PHASE_WORDS.get(t.phase, t.phase)
    return state_word(t.state)


def _active_table(store: Store, active: list[Task], live: dict[int, int], pulses: dict, ts: int,
                  w: int | None) -> tuple[str, list[str]]:
    """The active tasks: the column head and the rows (one line per task) — the overview caps them itself."""
    head = ["", _t("views.col_id"), _t("views.col_kind"), _t("views.col_title"), _t("views.col_state"),
            _t("views.col_model"), _t("views.col_round"), _t("views.col_idle"), _t("views.col_cost")]
    rows = []
    for t in active:
        go, usd = archive.task_cost(store, t.id)
        pl = pulses.get(t.id)
        mark = ui.badge(pl.mark, "", pl.state) if pl else ("⚫" if t.id not in live else "")
        rows.append([mark, t.label, t.kind.value, t.title, _state_cell(t), t.executor or "—",
                     str(t.round), age(t.updated_at, ts), f"${go + usd:.3f}"])
    lines = ui.table(head, rows, max_width=[1, 6, 7, None, 13, 10, 5, 8, 10], indent=2, w=w).split("\n")
    return lines[0], lines[1:]


def status_text(store: Store, *, project: str | None = None, live: dict[int, int] | None = None,
                now: int | None = None, pulses: dict | None = None, w: int | None = None) -> str:
    """L1: the counts, the active tasks as a table, what waits, the unread. ≤ 1500 bytes."""
    ts = now if now is not None else now_ms()
    live = live or {}
    pulses = pulses or {}
    active = store.list_tasks(states=ACTIVE, project=project)
    waiting = store.list_tasks(states=WAITING_DECISION, project=project)
    queued = store.list_tasks(states={State.QUEUED}, project=project)
    with store.read() as c:
        q_open = c.execute("SELECT COUNT(*) FROM question WHERE status='open'").fetchone()[0]
        msgs = c.execute("SELECT COUNT(*) FROM message WHERE direction='in' AND delivered_at IS NULL").fetchone()[0]
    unacked = events.unacked(store, project)
    tail = []
    if q_open:
        tail.append(_t("views.q_open", n=q_open))
    if msgs:
        tail.append(_t("views.msgs", n=msgs))
    if unacked:
        tail.append(_t("views.unacked", n=len(unacked)))
    if not (active or waiting or queued) and not tail:
        return _t("views.quiet")
    head = ""
    if active or waiting or queued:
        head = ui.styled(_t("views.head", project=project or _t("views.all_projects"), active=len(active),
                             waiting=len(waiting), queued=len(queued)), "bold")
    groups: list[tuple[str, list[str]]] = []
    if active:
        table_head, rows = _active_table(store, active, live, pulses, ts, w)
        groups.append((table_head, rows))  # the column head is the heading of the group
    waiting_rows = []
    if waiting or queued:
        block = ui.kv([(t.label, [state_word(t.state), reasons.text(t.state_reason)])
                       for t in waiting + queued], indent=2, w=w)
        waiting_rows = block.split("\n")
    if waiting_rows:
        groups.append((ui.section(_t("views.sec_waiting")), waiting_rows))
    if unacked:
        lines = events.lines(store, unacked[:UNREAD_LINES])
        groups.append((ui.section(_t("views.sec_unread")), ["  " + ln for ln in lines]))
    return _cap(head, groups, ui.styled(" · ".join(tail), "dim") if tail else "")


_SUT = re.compile(r"^##\s*(?:Суть|Summary)\s*$(.*?)(?=^##\s|\Z)", re.MULTILINE | re.DOTALL)


def report_essence(report: str, max_lines: int = 12) -> str:
    m = _SUT.search(report)
    body = (m.group(1) if m else report).strip()
    return "\n".join(body.splitlines()[:max_lines])


def _result_paths(t: Task) -> tuple[Path | None, Path | None]:
    if not t.worktree:
        return None, None
    base = Path(t.worktree) / workspace.AHUB_DIR
    return base / "result.json", base / "report.md"


def _cost_cell(go: float, usd: float, budget: float) -> str:
    cell = _t("views.cost_go", go=f"{go:.3f}")
    if usd:
        cell += " " + _t("views.cost_usd", usd=f"{usd:.3f}")
    if budget:
        cell += " " + _t("views.cost_budget", budget=f"{budget:.2f}")
    return cell


def _review_cell(t: Task) -> str:
    models = ", ".join(str(m) for m in (t.review.get("models") or []))
    rounds = int(t.review.get("rounds") or 0)
    return models + (_t("views.review_rounds", rounds=rounds) if rounds > 1 else "")


def open_findings(t: Task, limit: int = 5) -> tuple[list, int]:
    """(blocking findings of the last review round, how many more there are).

    The verdict files live in the task copy (`.ahub/review_r<N>_<model>.json`) — the same files the
    engine reads; an accepted task has no copy left, so nothing is shown for it.
    """
    from ahub import review

    if not t.worktree or t.state not in (State.DONE, State.NEEDS_DECISION, State.REVIEWING, State.FIXING):
        return [], 0
    try:
        rounds = sorted((int(m.group(1)) for p in Path(t.worktree, workspace.AHUB_DIR).glob("review_r*_*.json")
                         if (m := re.match(r"review_r(\d+)_", p.name))), reverse=True)
    except OSError:
        return [], 0
    if not rounds:
        return [], 0
    round_no = rounds[0]
    found = []
    for path in sorted(Path(t.worktree, workspace.AHUB_DIR).glob(f"review_r{round_no}_*.json")):
        model = path.stem.split("_", 2)[-1]
        rv = review.parse(path, model)
        if rv is not None:
            found += [f for f in rv.findings if f.severity != "low"]
    blocking = review.dedup(found)
    return blocking[:limit], max(0, len(blocking) - limit)


def _findings_lines(t: Task, w: int | None) -> list[str]:
    """The 'Review findings' block: severity, file:line, one wrapped line each."""
    findings, more = open_findings(t)
    if not findings:
        return []
    out = [ui.section(_t("views.sec_findings"))]
    rows = [[f.severity, f"{f.file}:{f.line}" if f.line else f.file, f.issue] for f in findings]
    out.append(ui.table(None, rows, max_width=[7, 28, None], indent=2, w=w))
    if more:
        out.append(ui.para(_t("views.findings_more", n=more, label=t.label), indent=2, w=w))
    return out


def task_text(store: Store, t: Task, *, live: dict[int, int] | None = None, now: int | None = None,
              w: int | None = None) -> str:
    """L2: the whole task, but brief — a header, the facts, the worker result. ≤ 4000 bytes."""
    ts = now if now is not None else now_ms()
    go, usd = archive.task_cost(store, t.id)
    title = ui.para(f"{t.label}  {t.kind.value}  {t.title}", indent=0, w=w)
    out = [title, ui.rule(min(ui.width(w), len(title.split("\n")[0])))]

    state = state_word(t.state)
    reason = reasons.text(t.state_reason)
    if reason:
        state += " · " + reason
    if t.state in ACTIVE and t.phase:
        state += " · " + PHASE_WORDS.get(t.phase, t.phase)
    if live and t.id in live:
        state += " · " + _t("views.alive")
    groups: list[list[tuple[str, Value]]] = [[(_t("views.lbl_state"), state)]]
    model: list[Any] = [t.executor or "—"]
    if t.review.get("models"):
        model.append((_t("views.lbl_review"), _review_cell(t)))
    if t.round > 1:
        model.append((_t("views.lbl_round"), str(t.round)))
    groups.append([(_t("views.lbl_model"), model)])
    if go or usd or t.budget_go:
        groups.append([(_t("views.lbl_cost"), _cost_cell(go, usd, t.budget_go))])
    since: list[Any] = [age(t.created_at, ts)]
    if t.after:
        since.append((_t("views.lbl_after"), ", ".join(f"T{a}" for a in t.after)))
    groups.append([(_t("views.lbl_age"), since)])
    out.extend(_facts(groups, w))

    rj, rp = _result_paths(t)
    if rj is not None and rj.exists():
        res = archive.read_json(rj)
        if res.get("summary"):
            out.append(ui.section(_t("views.sec_summary")))
            out.append(ui.para(ui.fit(str(res["summary"]), SUMMARY_BYTES, _t("views.more_at", label=t.label)),
                               indent=2, w=w))
        points = [str(q) for q in (res.get("questions") or [])[:3]]
        notes = str(res.get("notes") or "").strip()
        if points or notes:
            out.append(ui.section(_t("views.sec_points")))
            if points:
                out.append(ui.bullets([ui.fit(p, QUESTION_BYTES) for p in points], indent=2, w=w))
            if notes:
                out.append(ui.para(ui.fit(notes, NOTES_BYTES), indent=2, w=w))
    if rp is not None and rp.exists():
        rep = rp.read_text(encoding="utf-8", errors="replace")
        out.append(ui.section(_t("views.sec_report", kb=f"{len(rep.encode()) / 1024:.1f}")))
        out.append(ui.para(ui.fit(report_essence(rep), REPORT_BYTES), indent=2, w=w))
    out.extend(_findings_lines(t, w))
    if t.state in DECISION_STATES:
        out.append(_next_line(t, "views.next_decide", w))
    elif t.state in RESUME_STATES:
        out.append(_next_line(t, "views.next_resume", w))
    return clip_bytes("\n".join(out), L2_LIMIT)


def _facts(groups: list[list[tuple[str, Value]]], w: int | None) -> list[str]:
    """The fact lines of a task: the pairs that belong together share one aligned line (Model/Review/Round,
    Age/After), and a long value (the state, the cost) does not push the pair columns of another line.

    Every group is its own kv block — that is what keeps its columns local — and every label is padded to
    the width of the widest one, so the block has a single label column.
    """
    lw = max(len(label) for group in groups for label, _ in group)
    out = []
    for group in groups:
        block = ui.kv([(label.ljust(lw), value) for label, value in group], w=w)
        if block:
            out.append(block)
    return out


def _next_line(t: Task, key: str, w: int | None) -> str:
    """The 'Next' line — the decision commands, for a task whose decision is pending."""
    return ui.styled(ui.kv([(_t("views.lbl_next"), _t(key, label=t.label))], w=w), "dim")


def next_line(key: str, label: str = "", w: int | None = None) -> str:
    """The 'Next' line after a command did something — the commands that follow it."""
    return ui.styled(ui.kv([(_t("views.lbl_next"), _t(key, label=label))], w=w), "dim")


def result_text(store: Store, t: Task, *, full: bool = False, max_bytes: int = L3_DEFAULT,
                w: int | None = None) -> str:
    """L2 (default) or L3 (--full): whole report, paged by limit."""
    if not full:
        return task_text(store, t, w=w)
    rj, rp = _result_paths(t)
    parts = []
    if rj is not None and rj.exists():
        parts.append(rj.read_text(encoding="utf-8", errors="replace").strip())
    if rp is not None and rp.exists():
        parts.append(rp.read_text(encoding="utf-8", errors="replace"))
    if not parts:
        return _t("views.no_result", label=t.label, state=t.state.value)
    return clip_bytes("\n\n".join(parts), max_bytes)


def log_text(store: Store, t: Task, *, max_bytes: int = L3_DEFAULT) -> str:
    """L3: tail of raw logs from recent sessions."""
    out = []
    for s in store.list_sessions(t.id)[-3:]:
        if s.log_path and Path(s.log_path).exists():
            data = Path(s.log_path).read_bytes()[-max_bytes:].decode("utf-8", "replace")
            out.append(f"=== {s.role} {s.model} {s.external_id} ({s.status}/{s.outcome})\n{data}")
    return clip_bytes("\n".join(out) or _t("views.no_logs", label=t.label), max_bytes)


def history_text(store: Store, *, project: str | None = None, limit: int = 20, w: int | None = None) -> str:
    """The recently finished tasks as a table."""
    done = [t for t in store.list_tasks(project=project, newest_first=True)
            if t.state in HISTORY_STATES][:limit]
    if not done:
        return _t("task.history_empty")
    head = [_t("views.col_id"), _t("views.col_kind"), _t("views.col_title"), _t("views.col_state"),
            _t("views.col_round"), _t("views.col_cost"), _t("views.col_dur")]
    rows = []
    for t in done:
        go, usd = archive.task_cost(store, t.id)
        dur = age(t.created_at, t.finished_at) if t.finished_at else "—"
        rows.append([t.label, t.kind.value, t.title, state_word(t.state), str(t.round),
                     f"${go + usd:.3f}", dur])
    return ui.table(head, rows, max_width=[6, 7, None, 16, 5, 9, 13], indent=2, w=w)


def questions_text(rows: list[dict], *, w: int | None = None) -> str:
    """Open owner questions: number, the question, the options."""
    if not rows:
        return _t("comms.questions_empty")
    head = [_t("views.col_num"), _t("views.col_question"), _t("views.col_options")]
    body = [[f"#{r['id']}", str(r.get("text") or ""), ", ".join(str(o) for o in (r.get("options") or []))]
            for r in rows]
    return ui.table(head, body, max_width=[4, None, 30], indent=2, w=w)


def inbox_text(rows: list[dict], *, w: int | None = None) -> str:
    """Unread owner messages: number and the text."""
    if not rows:
        return _t("comms.inbox_empty")
    head = [_t("views.col_num"), _t("views.col_message")]
    body = [[f"#{r['id']}", str(r.get("text") or "")] for r in rows]
    return ui.table(head, body, max_width=[4, None], indent=2, w=w)
