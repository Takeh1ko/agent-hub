"""Что видит оркестратор: уровни L1 (сводка) и L2/L3 (задача, результат) с жёсткими лимитами (contracts §5)."""

from __future__ import annotations

import re
from pathlib import Path

from ahub import archive, events, workspace
from ahub.events import EVENT_CODES
from ahub.i18n import Words
from ahub.i18n import t as _t
from ahub.model import ACTIVE, Ev, State, WAITING_DECISION
from ahub.store import Store, Task
from ahub.time import now_ms

L1_LIMIT = 1500
L2_LIMIT = 4000
L3_DEFAULT = 20000
PHASE_WORDS: Words = Words("views.phase_", ("studying", "writing", "testing", "waiting"))
DECISION_WORDS = {State.DONE: EVENT_CODES[Ev.DONE], State.NEEDS_DECISION: EVENT_CODES[Ev.NEEDS_DECISION],
                  State.ERROR: EVENT_CODES[Ev.ERROR], State.STOPPED: "STOPPED"}


def clip_bytes(text: str, limit: int) -> str:
    b = text.encode("utf-8")
    if len(b) <= limit:
        return text
    cut = b[: limit - 4].decode("utf-8", "ignore")
    return cut.rsplit("\n", 1)[0] + "\n…" if "\n" in cut else cut + "…"


def _age(ms: int, now: int) -> str:
    m = max(0, (now - ms) // 60000)
    if m < 60:
        return _t("views.age_min", m=m)
    if m < 24 * 60:
        return _t("views.age_hm", h=m // 60, m=m % 60)
    return _t("views.age_dh", d=m // 1440, h=(m % 1440) // 60)


def _short(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def status_text(store: Store, *, project: str | None = None, live: dict[int, int] | None = None,
                now: int | None = None, pulses: dict | None = None) -> str:
    """L1: активные (с пульсом), ждущие решения, очередь, вопросы, непрочитанное. ≤ 1500 байт."""
    ts = now if now is not None else now_ms()
    live = live or {}
    lines: list[str] = []
    active = store.list_tasks(states=ACTIVE, project=project)
    for t in active:
        go, usd = archive.task_cost(store, t.id)
        pl = (pulses or {}).get(t.id)
        mark = f"{pl.mark} " if pl else ""
        why = f" · {_short(pl.reason, 50)}" if pl and pl.state != "working" and pl.reason else (
            "" if pl or t.id in live else _t("views.no_process"))
        phase = PHASE_WORDS.get(t.phase, t.state.value)
        lines.append(f"{mark}{t.label} {t.kind.value} «{_short(t.title, 40)}» · {phase} · {t.executor}"
                     f"{_t('views.round', round=t.round) if t.round > 1 else ''} · {_age(t.updated_at, ts)} · ${go + usd:.2f}{why}")
    waiting = [t for t in store.list_tasks(states=WAITING_DECISION, project=project)]
    for t in waiting:
        lines.append(f"{DECISION_WORDS[t.state]} {t.label} «{_short(t.title, 40)}» — {_short(t.state_reason, 70)}")
    queued = store.list_tasks(states={State.QUEUED}, project=project)
    if queued:
        reasons = "; ".join(f"{t.label}: {_short(t.state_reason, 30)}" for t in queued[:3] if t.state_reason)
        lines.append(_t("views.queued", n=len(queued)) + (f" ({reasons})" if reasons else ""))
    with store.read() as c:
        q_open = c.execute("SELECT COUNT(*) FROM question WHERE status='open'").fetchone()[0]
        msgs = c.execute("SELECT COUNT(*) FROM message WHERE direction='in' AND delivered_at IS NULL").fetchone()[0]
    unacked = len(events.unacked(store, project))
    tail = []
    if q_open:
        tail.append(_t("views.q_open", n=q_open))
    if msgs:
        tail.append(_t("views.msgs", n=msgs))
    if unacked:
        tail.append(_t("views.unacked", n=unacked))
    if tail:
        lines.append("; ".join(tail))
    if not lines:
        return _t("views.quiet")
    out, used = [], 0
    for i, ln in enumerate(lines):
        size = len(ln.encode("utf-8")) + 1
        if used + size > L1_LIMIT - 40:
            out.append(_t("views.more", n=len(lines) - i))
            break
        out.append(ln)
        used += size
    return "\n".join(out)


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


def task_text(store: Store, t: Task, *, live: dict[int, int] | None = None, now: int | None = None) -> str:
    """L2: задача целиком, но кратко. ≤ 4000 байт."""
    ts = now if now is not None else now_ms()
    go, usd = archive.task_cost(store, t.id)
    st = archive.STATE_WORDS.get(t.state.value, t.state.value)
    lines = [f"{t.label} {t.kind.value} «{t.title}»",
             _t("views.state", state=st) + (f" — {t.state_reason}" if t.state_reason else "")
             + (f" · {PHASE_WORDS.get(t.phase, t.phase)}" if t.phase else "")
             + (_t("views.alive") if live and t.id in live else ""),
             _t("views.model", executor=t.executor,
               review=', '.join(t.review.get('models', [])) or _t("archive.no_review"))
             + (f"×{t.review.get('rounds')}" if t.review else "")
             + _t("views.created", round=t.round, age=_age(t.created_at, ts)),
             _t("views.cost", go=f"{go:.3f}")
             + (_t("views.cost_real", usd=f"{usd:.3f}") if usd else "")
             + _t("views.cost_budget", budget=f"{t.budget_go:g}")]
    if t.after:
        lines.append(_t("views.after", items=", ".join(f"T{a}" for a in t.after)))
    rj, rp = _result_paths(t)
    if rj is not None and rj.exists():
        res = archive.read_json(rj)
        if res.get("summary"):
            lines.append(_t("views.summary", summary=_short(str(res['summary']), 400)))
        for q in (res.get("questions") or [])[:3]:
            lines.append(_t("views.question", text=_short(str(q), 200)))
        if res.get("notes"):
            lines.append(_t("views.notes", text=_short(str(res['notes']), 300)))
    if rp is not None and rp.exists():
        rep = rp.read_text(encoding="utf-8", errors="replace")
        lines.append(_t("views.report", kb=f"{len(rep.encode()) / 1024:.1f}"))
        lines.append(report_essence(rep))
    return clip_bytes("\n".join(lines), L2_LIMIT)


def result_text(store: Store, t: Task, *, full: bool = False, max_bytes: int = L3_DEFAULT) -> str:
    """L2 (по умолчанию) или L3 (--full): отчёт целиком, постранично по лимиту."""
    if not full:
        return task_text(store, t)
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
    """L3: хвост сырых логов последних сессий."""
    out = []
    for s in store.list_sessions(t.id)[-3:]:
        if s.log_path and Path(s.log_path).exists():
            data = Path(s.log_path).read_bytes()[-max_bytes:].decode("utf-8", "replace")
            out.append(f"=== {s.role} {s.model} {s.external_id} ({s.status}/{s.outcome})\n{data}")
    return clip_bytes("\n".join(out) or _t("views.no_logs", label=t.label), max_bytes)
