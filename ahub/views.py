"""What the orchestrator sees: L1 (status), L2 (task detail), history, questions, inbox (one message or
question in full — `ahub inbox <id>`), the levels and byte limits of contracts §5. Every block is scoped by
project (ahub/scope.py): `scope` — the project of the orchestrator's repository, None — every project (the
owner). The drawing primitives live in ahub/ui.py, the reason codes in ahub/reasons.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ahub import archive, events, reasons, ui, workspace
from ahub.i18n import Words, plural
from ahub.i18n import t as _t
from ahub.model import ACTIVE, FINAL, WAITING_DECISION, State
from ahub.scope import OWNER, Scope
from ahub.scope import where as scope_where  # the SQL condition of a scope
from ahub.store import Store, Task
from ahub.time import fmt_local, now_ms
from ahub.ui import Value

L1_LIMIT = 1500
L1_RESERVE = 120  # room for the "+N more" line and the counters under it
L2_LIMIT = 4000
L3_DEFAULT = 20000
SUMMARY_BYTES = 900  # the byte budget of every block in L2 (the whole L2 stays under 4 KB)
QUESTION_BYTES = 240
NOTES_BYTES = 400
REPORT_BYTES = 700
FINDINGS_BYTES = 1200  # the whole "Review findings" block (a long finding is worth its room, not its budget)
FINDING_BYTES = 400    # one issue / one fix
FINDING_INDENT = 10
FINDINGS_MAX = 6
UNREAD_LINES = 3
MSG_LINES = 2  # the lines of a message in the inbox list; the rest is under `ahub inbox <id>`
MSG_INDENT = 2
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


def age(ms: int, now: int) -> str:
    m = max(0, (now - ms) // 60000)
    if m < 60:
        return _t("views.age_min", m=m)
    if m < 24 * 60:
        return _t("views.age_hm", h=m // 60, m=m % 60)
    return _t("views.age_dh", d=m // 1440, h=(m % 1440) // 60)


_age = age


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


def state_cell(t: Task) -> str:
    """The state column: the phase of an active task (what it is doing), else the state."""
    if t.state in ACTIVE and t.phase:
        return PHASE_WORDS.get(t.phase, t.phase)
    return state_word(t.state)


_state_cell = state_cell


def _pulse_detail(pl) -> str:
    """What a task is doing under its ⏺ line: the tool it runs, why it waits, or that it went quiet.
    The pulse brings its own colour (green/yellow/red) — the mark and the words together."""
    if pl is None or not pl.reason:
        return ""
    return ui.badge(pl.mark, pl.reason, pl.state)


def _item_tail(t: Task, ts: int, cost: str = "") -> str:
    """The dim right-aligned tail of an item: what it is doing · model · idle · cost."""
    return " · ".join([x for x in (_state_cell(t), t.executor or "—", _age(t.updated_at, ts), cost) if x])


def _waiting_item(t: Task, w: int | None) -> str:
    """A task that waits a person: its state and reason on the item line, the exact commands under it."""
    reason = reasons.text(t.state_reason)
    head = f"{t.label}  {state_word(t.state)}" + (f" · {reason}" if reason else "")
    nxt = _t("views.hint_next", cmd=_t(next_key(t), label=t.label)) if _offers_next(t) else ""
    return ui.item(head, [nxt], w=w)


def _offers_next(t: Task) -> bool:
    return t.state in DECISION_STATES or t.state in RESUME_STATES


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
                     str(t.round), _age(t.updated_at, ts), f"${go + usd:.3f}"])
    lines = ui.table(head, rows, max_width=[1, 6, 7, None, 13, 10, 5, 8, 10], indent=2, w=w).split("\n")
    return lines[0], lines[1:]


def _active_items(store: Store, active: list[Task], pulses: dict, ts: int, w: int | None) -> list[str]:
    """The active tasks as items: the mark, the id and the title, the tail right-aligned, the pulse under it."""
    out = []
    for t in active:
        go, usd = archive.task_cost(store, t.id)
        out.append(ui.item(f"{t.label}  {t.title}", [_pulse_detail(pulses.get(t.id))],
                           tail=_item_tail(t, ts, f"${go + usd:.3f}"), w=w))
    return out


def status_text(store: Store, *, scope: Scope | None = None, live: dict[int, int] | None = None,
                now: int | None = None, pulses: dict | None = None, w: int | None = None) -> str:
    """L1: the counts, the active tasks, what waits, the unread — of the scope. ≤ 1500 bytes.

    A terminal reads them as ⏺ items; a pipe gets the compact table (contracts §5 — the orchestrator
    reads that one).
    """
    sc = scope or OWNER
    ts = now if now is not None else now_ms()
    live = live or {}
    pulses = pulses or {}
    active = store.list_tasks(states=ACTIVE, projects=sc.projects)
    waiting = store.list_tasks(states=WAITING_DECISION, projects=sc.projects)
    queued = store.list_tasks(states={State.QUEUED}, projects=sc.projects)
    cond, args = scope_where(sc)
    with store.read() as c:
        q_sql = "SELECT COUNT(*) FROM question WHERE status='open'"
        m_sql = "SELECT COUNT(*) FROM message WHERE direction='in' AND delivered_at IS NULL"
        if cond:
            q_sql += " AND " + cond
            m_sql += " AND " + cond
        q_open = c.execute(q_sql, args).fetchone()[0]
        msgs = c.execute(m_sql, args).fetchone()[0]
    unacked = events.unacked(store, sc)
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
        head = ui.styled(_t("views.head", project=sc.name or _t("views.all_projects"), active=len(active),
                             waiting=len(waiting), queued=len(queued)), "bold")
    groups: list[tuple[str, list[str]]] = []
    if active:
        if ui.colour_on():
            groups.append(("", _active_items(store, active, pulses, ts, w)))
        else:
            table_head, rows = _active_table(store, active, live, pulses, ts, w)
            groups.append((table_head, rows))  # the column head is the heading of the group
    if waiting or queued:
        rest = waiting + queued
        if ui.colour_on():
            groups.append(("", [_waiting_item(t, w) for t in rest]))
        else:
            groups.append((ui.section(_t("views.sec_waiting")),
                           ui.kv([(t.label, [state_word(t.state), reasons.text(t.state_reason)])
                                  for t in rest], indent=2, w=w).split("\n")))
    if unacked:
        lines = events.lines(store, unacked[:UNREAD_LINES])
        head_of = ui.section(_t("views.sec_unread"))
        groups.append((head_of, [ui.hint(ln, w=w) if ui.colour_on() else "  " + ln for ln in lines]))
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
    engine reads; an accepted or rejected task has no copy left, so there is nothing to show.
    """
    from ahub import review

    if not t.worktree or t.state in FINAL:
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
    """The 'Review findings' block: severity and file:line on their own line, the whole issue and its fix
    wrapped under them — a finding is worth reading, so nothing of it is cut to a table cell.

    The block has its own byte budget (FINDINGS_BYTES): the L2 cap is 4 KB and the summary, the report and
    the facts come first — what does not fit here is counted, not truncated mid-sentence.
    """
    findings, more = open_findings(t, limit=FINDINGS_MAX)
    if not findings:
        return []
    out, used = [ui.section(_t("views.sec_findings"))], 0
    for i, f in enumerate(findings):
        head = f"{f.severity:<6} {f.file}:{f.line}" if f.line else f"{f.severity:<6} {f.file}"
        block = [ui.hint(head, w=w) if ui.colour_on() else "  " + head,
                 ui.para(ui.fit(f.issue, FINDING_BYTES), indent=FINDING_INDENT, w=w)]
        if f.fix:
            block.append(ui.para(_t("views.finding_fix", fix=ui.fit(f.fix, FINDING_BYTES)),
                                 indent=FINDING_INDENT, w=w))
        size = sum(len(ln.encode()) + 1 for ln in block)
        if used + size > FINDINGS_BYTES and i:
            more += len(findings) - i  # every finding that does not fit — not just this one
            break
        out += block
        used += size
    if more:
        out.append(_point(plural(more, "views.findings_more_one", "views.findings_more_few",
                                 "views.findings_more", label=t.label), 2, w))
    return out


def _point(text: str, indent: int, w: int | None) -> str:
    """A secondary line of a block: a ⎿ detail on a terminal, an indented paragraph in a pipe."""
    return ui.hint(text, indent=indent, w=w) if ui.colour_on() else ui.para(text, indent=indent, w=w)


def task_text(store: Store, t: Task, *, live: dict[int, int] | None = None, now: int | None = None,
              w: int | None = None) -> str:
    """L2: the whole task, but brief — a header, the facts, the worker result. ≤ 4000 bytes."""
    ts = now if now is not None else now_ms()
    go, usd = archive.task_cost(store, t.id)
    head_text = f"{t.label}  {t.kind.value}  {t.title}"
    title = ui.item(head_text, w=w) if ui.colour_on() else ui.para(head_text, indent=0, w=w)
    out = [title, ui.rule(min(ui.width(w), ui.plain_len(title.split("\n")[0])))]

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
    since: list[Any] = [_age(t.created_at, ts)]
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
            if points:  # open points — one ⎿ line each on a terminal, one bullet each in a pipe
                shown = [ui.fit(p, QUESTION_BYTES) for p in points]
                out += [ui.hint(p, w=w) for p in shown] if ui.colour_on() \
                    else [ui.bullets(shown, indent=2, w=w)]
            if notes:
                out.append(ui.para(ui.fit(notes, NOTES_BYTES), indent=2, w=w))
    if rp is not None and rp.exists():
        rep = rp.read_text(encoding="utf-8", errors="replace")
        out.append(ui.section(_t("views.sec_report", kb=f"{len(rep.encode()) / 1024:.1f}")))
        out.append(ui.para(ui.fit(report_essence(rep), REPORT_BYTES), indent=2, w=w))
    out.extend(_findings_lines(t, w))
    # the Next line is booked before the clip: the decision commands are the point of the screen, so what
    # does not fit is the tail of the blocks above them (the findings), never the way out
    nxt = next_line(next_key(t), t.label, w) if _offers_next(t) else ""
    body = clip_bytes("\n".join(out), L2_LIMIT - (len(nxt.encode()) + 1 if nxt else 0))
    return f"{body}\n{nxt}" if nxt else body


def _facts(groups: list[list[tuple[str, Value]]], w: int | None) -> list[str]:
    """The fact lines of a task: the pairs that belong together share one aligned line (Model/Review/Round,
    Age/After), and a long value (the state, the cost) does not push the pair columns of another line.

    Every group is its own kv block — that is what keeps its columns local — and every label is padded to
    the width of the widest one, so the block has a single label column. On a terminal the whole card is
    secondary text (grey).
    """
    lw = max(len(label) for group in groups for label, _ in group)
    out = []
    for group in groups:
        block = ui.kv([(label.ljust(lw), value) for label, value in group], w=w, dim=ui.colour_on())
        if block:
            out.append(block)
    return out


def next_key(t: Task) -> str:
    """The commands that fit the state: a decision (done / needs decision) or a resume (error / stopped)."""
    return "views.next_decide" if t.state in DECISION_STATES else "views.next_resume"


def next_line(key: str, label: str = "", w: int | None = None) -> str:
    """The 'Next' line — the way out: a ⎿ detail under the item on a terminal, an aligned dim line in a pipe."""
    cmd = _t(key, label=label)
    if ui.colour_on():
        return ui.hint(_t("views.hint_next", cmd=cmd), w=w)
    return ui.styled(ui.kv([(_t("views.lbl_next"), cmd)], w=w), "dim")


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


def history_text(store: Store, *, scope: Scope | None = None, limit: int = 20, w: int | None = None) -> str:
    """The recently finished tasks of the scope as a table."""
    done = [t for t in store.list_tasks(projects=(scope or OWNER).projects, newest_first=True)
            if t.state in HISTORY_STATES][:limit]
    if not done:
        return _t("task.history_empty")
    head = [_t("views.col_id"), _t("views.col_kind"), _t("views.col_title"), _t("views.col_state"),
            _t("views.col_round"), _t("views.col_cost"), _t("views.col_dur")]
    rows = []
    for t in done:
        go, usd = archive.task_cost(store, t.id)
        dur = _age(t.created_at, t.finished_at) if t.finished_at else "—"
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


def question_text(row: dict, *, w: int | None = None) -> str:
    """One owner question in full (L3): the header, the facts, the text, the options, the answer."""
    title = f"#{row['id']}"
    out = [title, ui.rule(min(ui.width(w), len(title)))]
    out.extend(_facts([_row_facts(row)], w))
    text = str(row.get("text") or "")
    if text.strip():
        out.append(ui.para(text, indent=MSG_INDENT, w=w))
    options = [str(o) for o in (row.get("options") or [])]
    if options:
        out.append(_t("views.lbl_options"))
        out.append(ui.bullets(options, indent=MSG_INDENT, w=w))
    if str(row.get("answer") or ""):
        out.append(ui.kv([(_t("views.lbl_answer"), str(row["answer"]))], indent=MSG_INDENT, w=w))
    return "\n".join(out)


def _row_facts(row: dict) -> list[tuple[str, Value]]:
    """The facts of an owner message or question: when it came, whose it is, where it came from.
    A row with no project is hub-wide, and one with no chat came from a command, not from Telegram."""
    facts: list[tuple[str, Value]] = [(_t("views.lbl_when"), fmt_local(int(row.get("ts") or 0)))]
    if row.get("project"):
        facts.append((_t("views.lbl_project"), str(row["project"])))
    if row.get("task_id"):  # a question may be about a task; a message has no task of its own
        facts.append((_t("views.lbl_task"), f"T{row['task_id']}"))
    if row.get("chat_id"):
        facts.append((_t("views.lbl_chat"), str(row["chat_id"])))
    return facts


def message_text(row: dict, *, w: int | None = None) -> str:
    """One owner message in full (L3): the header, the facts, the whole text (nothing of it cut)."""
    title = f"#{row['id']}"
    out = [title, ui.rule(min(ui.width(w), len(title)))]
    out.extend(_facts([_row_facts(row)], w))
    text = str(row.get("text") or "")
    if text.strip():
        out.append(ui.para(text, indent=MSG_INDENT, w=w))
    return "\n".join(out)


def _head(text: str, body: int, lines: int) -> tuple[list[str], bool]:
    """The first `lines` lines of a text, wrapped, and whether something is left of it.

    para does not break a long word (a URL in a Telegram message) — a line is clipped to the column, and a
    clipped line is a cut too, so the hint says where the rest is.
    """
    wrapped = ui.para(text, indent=0, w=body).split("\n")
    head = wrapped[:lines]
    return ([ui.clip(ln, body) for ln in head],
            len(wrapped) > lines or any(len(ln) > body for ln in head))


def inbox_text(rows: list[dict], *, full: bool = False, w: int | None = None) -> str:
    """Unread owner messages: number and the head of the text; the whole one is `ahub inbox <id>`.

    full — every message in full, one block per message (what the MCP tool reads).
    """
    if not rows:
        return _t("comms.inbox_empty")
    if full:
        return "\n\n".join(message_text(r, w=w) for r in rows)
    nw = max(len(f"#{r['id']}") for r in rows)
    body = max(20, ui.width(w) - MSG_INDENT - nw - ui.GAP)
    pad, cell = " " * MSG_INDENT, " " * MSG_INDENT + " " * (nw + ui.GAP)
    out = [(pad + _t("views.col_num").ljust(nw) + " " * ui.GAP + _t("views.col_message")).rstrip()]
    for r in rows:
        lines, rest = _head(str(r.get("text") or ""), body, MSG_LINES)
        out.append((pad + f"#{r['id']}".ljust(nw + ui.GAP) + lines[0]).rstrip())
        out.extend((cell + ln).rstrip() for ln in lines[1:])
        if rest:
            out.append(cell + _t("views.more_msg", id=r["id"]))
    return "\n".join(out)
