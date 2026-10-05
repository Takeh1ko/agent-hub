#!/usr/bin/env python3
"""Build docs/assets/agent-hub-motion.svg — the motion piece at the top of the README.

One self-contained animated SVG: CSS keyframes only (no script, so GitHub renders it through <img>), the fonts
subset and embedded as data URIs (an SVG inside <img> loads nothing external). Every animation on the main
timeline shares the loop length T, so the whole piece stays in sync on every repeat.

The story, in five scenes:
  A  Claude Code gets a big task and files four `ahub task new` commands;
  B  the window steps aside, the hub dispatches the tasks to four worker cards;
  C  the workers write the code — tool calls, test runs, line counters, the money ticker;
  D  gates and the review panel pass, DONE wakes Claude, Claude accepts each task;
  E  the bill: Claude writing every line itself vs. Claude orchestrating cheap workers.

Run: python3 tools/readme_motion/build.py  (needs fonttools + brotli; writes the SVG next to the README assets).
"""
from __future__ import annotations

import base64
import html
import io
from dataclasses import dataclass, field
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

HERE = Path(__file__).resolve().parent
OUT = HERE.parent.parent / "docs" / "assets" / "agent-hub-motion.svg"

T = 26.0                      # loop length, seconds
W, H = 1200, 675
EASE = "cubic-bezier(.22,.8,.24,1)"
EASE_IO = "cubic-bezier(.65,0,.35,1)"

# palette — dark, warm accent; Claude's orange for Claude, ahub's 208 orange for the hub
BG = "#09090b"
WIN = "#131317"
WIN_EDGE = "#26262e"
CARD = "#111115"
TEXT = "#e7e5e4"
SOFT = "#b4b4bd"
DIM = "#83838d"
FAINT = "#4b4b55"
CLAUDE = "#d97757"
ACCENT = "#ff8a3d"
GREEN = "#4ade80"
RED = "#f87171"
YELLOW = "#fbbf24"
BLUE = "#7dd3fc"
VIOLET = "#a78bfa"
PINK = "#f9a8d4"
PROMPT_BG = "#1c1c22"

MONO = "M, MS, ui-monospace, SFMono-Regular, Menlo, monospace"
SANS = "S, MS, system-ui, -apple-system, Segoe UI, sans-serif"
CW = 0.6                      # JetBrains Mono advance, em

USED: set[str] = set()


def esc(s: str) -> str:
    USED.update(s)
    return html.escape(s, quote=False)


# --- animation registry -------------------------------------------------------------------------------------

class Anims:
    """Collects @keyframes; each add() returns a class name that plays it on the shared loop."""

    def __init__(self) -> None:
        self.css: list[str] = []
        self.n = 0

    def add(self, frames: list[tuple], period: float = T, timing: str = "linear") -> str:
        self.n += 1
        name = f"k{self.n}"
        fr = sorted(frames, key=lambda f: f[0])
        if fr[0][0] > 0:
            fr.insert(0, (0.0, fr[0][1]))
        if fr[-1][0] < period:
            fr.append((period, fr[-1][1]))
        rows = []
        for f in fr:
            p = min(100.0, max(0.0, f[0] / period * 100))
            tf = f"animation-timing-function:{f[2]};" if len(f) > 2 and f[2] else ""
            rows.append(f"{p:.3f}%{{{f[1]};{tf}}}")
        self.css.append(f"@keyframes {name}{{{''.join(rows)}}}"
                        f".{name}{{animation:{name} {period:g}s {timing} infinite both}}")
        return name


A = Anims()
EPS = 0.02


def appear(t: float, dur: float = 0.35, dy: float = 0.0, t_out: float | None = None, out: float = 0.35,
           dx: float = 0.0) -> str:
    """Fade (and slide) in at t, optionally out at t_out."""
    hide = f"opacity:0;transform:translate({dx}px,{dy}px)"
    show = "opacity:1;transform:translate(0px,0px)"
    fr = [(0, hide), (t, hide, EASE), (t + dur, show)]
    if t_out is not None:
        fr += [(t_out, show, EASE_IO), (t_out + out, "opacity:0;transform:translate(0px,0px)")]
    return A.add(fr)


def visible(t0: float, t1: float | None) -> str:
    """Hard cut: shown from t0 to t1 (discrete values: counters, chips)."""
    fr = [(0, "opacity:0"), (max(t0 - EPS, 0), "opacity:0"), (t0, "opacity:1")]
    if t1 is not None:
        fr += [(t1 - EPS, "opacity:1"), (t1, "opacity:0")]
    return A.add(fr)


def draw(t0: float, t1: float, length: float = 100, back: float | None = None) -> str:
    """stroke-dashoffset from length to 0 between t0 and t1 (a line drawing itself)."""
    fr = [(0, f"stroke-dashoffset:{length}"), (t0, f"stroke-dashoffset:{length}", EASE_IO),
          (t1, "stroke-dashoffset:0")]
    return A.add(fr)


def progress(points: list[tuple[float, float]], length: float = 100) -> str:
    """A bar that grows through (t, fraction) points."""
    fr = [(0, f"stroke-dashoffset:{length}")]
    for t, f in points:
        fr.append((t, f"stroke-dashoffset:{length * (1 - f):.2f}", "ease-out"))
    return A.add(fr)


def pulse(t0: float, dur: float = 0.9) -> str:
    """One bright dash travelling along a path (pathLength=100, dasharray '10 110')."""
    fr = [(0, "stroke-dashoffset:10;opacity:0"), (t0, "stroke-dashoffset:10;opacity:1", "ease-in-out"),
          (t0 + dur, "stroke-dashoffset:-100;opacity:1"), (t0 + dur + EPS, "stroke-dashoffset:-100;opacity:0")]
    return A.add(fr)


# --- text ---------------------------------------------------------------------------------------------------

Span = tuple  # (text, colour[, bold])


def spans_svg(spans: list[Span]) -> str:
    out = []
    for s in spans:
        txt, col = s[0], s[1]
        bold = len(s) > 2 and s[2]
        w = ' font-weight="700"' if bold else ""
        out.append(f'<tspan fill="{col}"{w}>{esc(txt)}</tspan>')
    return "".join(out)


def mono_line(x: float, y: float, spans: list[Span], size: float) -> str:
    """One terminal line. ⏺ and ⎿ are drawn (no font has them at this weight), the rest is text."""
    cw = size * CW
    shapes, clean, col = [], [], 0
    for s in spans:
        txt = s[0]
        buf = []
        for ch in txt:
            cx = x + col * cw
            if ch == "⏺":
                shapes.append(f'<circle cx="{cx + cw / 2:.1f}" cy="{y - size * 0.32:.1f}" r="{size * 0.27:.1f}" '
                              f'fill="{s[1]}"/>')
                buf.append(" ")
            elif ch == "⎿":
                shapes.append(f'<path d="M{cx + cw * 0.35:.1f} {y - size * 0.95:.1f}V{y - size * 0.3:.1f}'
                              f'H{cx + cw * 1.05:.1f}" fill="none" stroke="{FAINT}" stroke-width="1.1"/>')
                buf.append(" ")
            else:
                buf.append(ch)
            col += 1
        clean.append((("".join(buf)),) + tuple(s[1:]))
    text = (f'<text x="{x}" y="{y}" font-family="{MONO}" font-size="{size}" xml:space="preserve" '
            f'style="white-space:pre">{spans_svg(clean)}</text>')
    return "".join(shapes) + text


def sans(x: float, y: float, txt: str, size: float, fill: str, weight: int = 400, anchor: str = "start",
         extra: str = "") -> str:
    return (f'<text x="{x}" y="{y}" font-family="{SANS}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}" {extra}>{esc(txt)}</text>')


def mono(x: float, y: float, txt: str, size: float, fill: str, weight: int = 400, anchor: str = "start",
         extra: str = "") -> str:
    return (f'<text x="{x}" y="{y}" font-family="{MONO}" font-size="{size}" font-weight="{weight}" '
            f'fill="{fill}" text-anchor="{anchor}" xml:space="preserve" style="white-space:pre" {extra}>'
            f'{esc(txt)}</text>')


def discrete(values: list[tuple[float, str]], end: float | None, render) -> str:
    """A value that changes in steps: values = [(t, payload)], each shown until the next one."""
    out = []
    for i, (t, v) in enumerate(values):
        t1 = values[i + 1][0] if i + 1 < len(values) else end
        out.append(f'<g class="{visible(t, t1)}">{render(v)}</g>')
    return "".join(out)


# --- a scrolling terminal -----------------------------------------------------------------------------------

@dataclass
class Line:
    t: float
    spans: list = field(default_factory=list)
    typing: float = 0.0          # > 0: typed out with a block cursor over this many seconds
    cover: str = WIN             # what the line sits on (the typing cover paints it)


class Term:
    """Lines that appear at their time; the view scrolls up once more lines than `rows` have appeared."""

    def __init__(self, x: float, y: float, w: float, rows: int, size: float, lh: float, uid: str) -> None:
        self.x, self.y, self.w, self.rows, self.size, self.lh, self.uid = x, y, w, rows, size, lh, uid

    def render(self, lines: list[Line], cursor: str = TEXT, under: str = "") -> str:
        cw = self.size * CW
        parts = [under]
        for i, ln in enumerate(lines):
            base = self.y + i * self.lh + self.size
            body = mono_line(self.x, base, ln.spans, self.size)
            if ln.typing > 0:
                n = sum(len(s[0]) for s in ln.spans)
                top = base - self.size * 1.02
                hgt = self.lh
                move = A.add([(0, "transform:translate(0px,0px)"),
                              (ln.t, "transform:translate(0px,0px)", f"steps({n},end)"),
                              (ln.t + ln.typing, f"transform:translate({n * cw:.2f}px,0px)")])
                blink = A.add([(0, "opacity:1"), (ln.t + ln.typing + 0.5, "opacity:1"),
                               (ln.t + ln.typing + 0.5 + EPS, "opacity:0")])
                body += (f'<svg x="{self.x:.2f}" y="{top:.2f}" width="{(n + 1) * cw + 1:.2f}" height="{hgt:.2f}" '
                         f'overflow="hidden"><g class="{move}">'
                         f'<rect x="0" y="1" width="{cw:.2f}" height="{hgt - 2:.2f}" fill="{cursor}" '
                         f'class="{blink}"/>'
                         f'<rect x="{cw:.2f}" y="0" width="{n * cw + 1:.2f}" height="{hgt:.2f}" fill="{ln.cover}"/>'
                         f'</g></svg>')
            parts.append(f'<g class="{appear(ln.t, 0.18 if ln.typing else 0.3, 0 if ln.typing else 4)}">{body}</g>')
        # scroll: when line i (i >= rows) appears, the view moves so it is the last visible one
        fr = [(0, "transform:translate(0px,0px)")]
        shift = 0
        for i, ln in enumerate(lines):
            need = max(0, i - self.rows + 1)
            if need > shift:
                fr.append((ln.t - 0.001, f"transform:translate(0px,{-shift * self.lh:.2f}px)", EASE))
                fr.append((ln.t + 0.28, f"transform:translate(0px,{-need * self.lh:.2f}px)"))
                shift = need
        scroll = A.add(fr)
        clip = (f'<clipPath id="c{self.uid}"><rect x="{self.x - 4}" y="{self.y}" width="{self.w + 8}" '
                f'height="{self.rows * self.lh + 2}"/></clipPath>')
        return f'{clip}<g clip-path="url(#c{self.uid})"><g class="{scroll}">{"".join(parts)}</g></g>'


# --- the story ----------------------------------------------------------------------------------------------

@dataclass
class Worker:
    key: str
    task: str
    title: str
    short: str
    review: str
    review_col: str
    added: int
    removed: int
    cost: float
    done: float
    work: list           # (text spans) tool lines, spread over the working time
    rework: list = field(default_factory=list)  # lines after a review finding (W3)


WORK_START = 8.6
WORKERS = [
    Worker("web-w1", "T165", "server + JSON API", "server + JSON API", "gemini", BLUE, 1284, 96, 0.31, 13.4, [
        [("✎ write ", DIM), ("ahub/web/server.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/api.py", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_server.py", SOFT)],
        [("  2 failed", RED), (", 9 passed in 1.71s", DIM)],
        [("✎ edit  ", DIM), ("ahub/web/server.py", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_server.py", SOFT)],
        [("  11 passed", GREEN), (" in 1.84s", DIM)],
        [("✎ write ", DIM), ("ahub/commands/web.py", SOFT)],
        [("▶ bash  ", DIM), ("git commit -m \"ahub web: server\"", SOFT)],
    ]),
    Worker("web-w2", "T166", "live task board", "task board", "bunny", PINK, 836, 12, 0.19, 14.3, [
        [("✎ write ", DIM), ("ahub/web/pages/board.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/board.js", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/board.css", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_board.py", SOFT)],
        [("  7 passed", GREEN), (" in 0.92s", DIM)],
        [("✎ edit  ", DIM), ("ahub/web/static/board.js", SOFT)],
        [("▶ bash  ", DIM), ("git commit -m \"board: live task board\"", SOFT)],
    ]),
    Worker("web-w3", "T167", "task page + live transcript", "task page + transcript", "gemini", BLUE, 1102, 41,
           0.33, 15.6, [
        [("✎ write ", DIM), ("ahub/web/pages/task.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/task.js", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_task.py", SOFT)],
        [("  9 passed", GREEN), (" in 1.12s", DIM)],
        [("▶ bash  ", DIM), ("git commit -m \"task page\"", SOFT)],
    ], rework=[
        [("✗ review gemini: ", RED), ("innerHTML on model text", SOFT)],
        [("✎ edit  ", DIM), ("ahub/web/static/task.js:88", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_task.py", SOFT)],
        [("  10 passed", GREEN), (" in 1.15s", DIM)],
    ]),
    Worker("web-w4", "T168", "actions, money panel, docs", "actions + money", "bunny", PINK, 996, 58, 0.21, 16.4, [
        [("✎ write ", DIM), ("ahub/web/pages/actions.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/pages/money.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/actions.js", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_actions.py", SOFT)],
        [("  12 passed", GREEN), (" in 2.03s", DIM)],
        [("✎ edit  ", DIM), ("docs/ARCHITECTURE.md", SOFT)],
        [("✎ edit  ", DIM), ("README.md", SOFT)],
        [("▶ bash  ", DIM), ("git commit -m \"actions + money\"", SOFT)],
    ]),
]
SHAS = ["4c1d9e2", "8e21f0a", "b07a3d5", "19fd6c8"]
TOTAL_ADDED = sum(w.added for w in WORKERS)
TOTAL_COST = sum(w.cost for w in WORKERS)
CLAUDE_ONLY = 130          # the same tokens at Claude API list prices, rounded (README footnote)
STAGE_OUT = 18.6
FINALE = 19.2


def worker_schedule(w: Worker, i: int) -> dict:
    """Times of a worker's phases: working → checking → reviewing (→ fixing → reviewing) → done → merged."""
    start = WORK_START + i * 0.18
    s = {"start": start, "done": w.done, "merged": w.done + 1.25}
    if w.rework:
        s["check"] = w.done - 4.0
        s["review"] = w.done - 3.3
        s["fix"] = w.done - 2.6
        s["review2"] = w.done - 0.9
        s["work_end"] = s["check"] - 0.2
    else:
        s["check"] = w.done - 1.8
        s["review"] = w.done - 0.95
        s["work_end"] = s["check"] - 0.2
    return s


def card(w: Worker, i: int, x: float, y: float, cw: float, ch: float) -> str:
    s = worker_schedule(w, i)
    out = [f'<g transform="translate({x},{y})">']
    inner = []
    inner.append(f'<rect x="0" y="0" width="{cw}" height="{ch}" rx="11" fill="{CARD}" stroke="{WIN_EDGE}"/>')
    # done: the edge turns green, a soft glow
    inner.append(f'<rect x="0.5" y="0.5" width="{cw - 1}" height="{ch - 1}" rx="11" fill="none" stroke="{GREEN}" '
                 f'stroke-opacity=".55" class="{appear(s["done"], 0.4)}"/>')
    # model badge (a monogram, not a logo)
    inner.append(f'<circle cx="24" cy="25" r="13" fill="url(#gSpark)"/>')
    inner.append(sans(24, 29.5, "S", 13, "#fff", 800, "middle"))
    inner.append(f'<text x="46" y="24" font-family="{SANS}" font-size="14" font-weight="600" fill="{TEXT}">'
                 f'{esc("Spark 1.3")}<tspan font-family="{MONO}" font-size="11" font-weight="400" fill="{DIM}">'
                 f'{esc(f"  {w.task} · review ")}</tspan><tspan font-family="{MONO}" font-size="11" '
                 f'fill="{w.review_col}">{esc(w.review)}</tspan></text>')
    inner.append(sans(46, 42, f"ahub web: {w.title}", 12, SOFT, 400))
    # state chip
    states = [(s["start"], "working", SOFT), (s["check"], "checking", YELLOW), (s["review"], "reviewing", BLUE)]
    if w.rework:
        states += [(s["fix"], "fixing", RED), (s["review2"], "reviewing", BLUE)]
    states += [(s["done"], "done ✓", GREEN), (s["merged"], "merged", VIOLET)]

    def chip(v):
        label, col = v
        wdt = len(label) * 6.6 + 18
        return (f'<rect x="{cw - 14 - wdt:.1f}" y="12" width="{wdt:.1f}" height="20" rx="10" fill="{col}" '
                f'fill-opacity=".12" stroke="{col}" stroke-opacity=".35"/>'
                + mono(cw - 14 - wdt / 2, 26, label, 11, col, 400, "middle"))
    inner.append(discrete([(t, (lb, c)) for t, lb, c in states], None, chip))
    # line counter, right of the title
    steps = 9
    vals = []
    for k in range(steps + 1):
        f = k / steps
        t = s["start"] + 0.4 + (s["work_end"] - s["start"] - 0.4) * f
        a = int(round(w.added * (f ** 1.15) / 7)) * 7 if k < steps else w.added
        r = int(round(w.removed * f)) if k < steps else w.removed
        vals.append((t, (a, r)))

    def counter(v):
        a, r = v
        return (f'<text x="{cw - 16}" y="42" font-family="{MONO}" font-size="11.5" text-anchor="end">'
                f'<tspan fill="{GREEN}">{esc(f"+{a:,}")}</tspan><tspan fill="{RED}" fill-opacity=".8">'
                f'{esc(f" −{r}")}</tspan></text>')
    inner.append(f'<g class="{appear(s["start"] + 0.4, 0.3)}">' + discrete(vals, None, counter) + "</g>")
    # the transcript
    lines: list[Line] = []
    n = len(w.work)
    span = s["work_end"] - s["start"] - 0.3
    for k, sp in enumerate(w.work):
        lines.append(Line(s["start"] + 0.3 + span * k / max(1, n - 1) * 0.97, sp, cover=CARD))
    lines.append(Line(s["check"] + 0.15, [("✓ gates ", GREEN), ("commit · diff ⊆ paths · tests", DIM)], cover=CARD))
    if w.rework:
        lines.append(Line(s["fix"] - 0.05, w.rework[0], cover=CARD))
        for k, sp in enumerate(w.rework[1:]):
            lines.append(Line(s["fix"] + 0.35 + k * 0.42, sp, cover=CARD))
        lines.append(Line(s["review2"] + 0.1, [("◆ review ", DIM), (w.review, w.review_col), (" · round 2", DIM)],
                          cover=CARD))
    else:
        lines.append(Line(s["review"] + 0.1, [("◆ review ", DIM), (w.review, w.review_col),
                                              (" · fresh session", DIM)], cover=CARD))
    lines.append(Line(s["done"] - 0.1, [("✓ approve", GREEN), (" · no findings", DIM)], cover=CARD))
    term = Term(16, 56, cw - 32, 4, 11.5, 15.6, f"w{i}")
    inner.append(term.render(lines))
    # progress bar
    pts = [(s["start"] + 0.3, 0.04), (s["work_end"], 0.72), (s["check"] + 0.6, 0.82), (s["done"], 1.0)]
    if w.rework:
        pts = [(s["start"] + 0.3, 0.04), (s["work_end"], 0.66), (s["review"], 0.74), (s["fix"] + 1.2, 0.86),
               (s["done"], 1.0)]
    y_bar = ch - 9
    inner.append(f'<line x1="16" y1="{y_bar}" x2="{cw - 16}" y2="{y_bar}" stroke="#1f1f26" stroke-width="2" '
                 f'stroke-linecap="round"/>')
    inner.append(f'<line x1="16" y1="{y_bar}" x2="{cw - 16}" y2="{y_bar}" stroke="url(#gBar)" stroke-width="2" '
                 f'stroke-linecap="round" pathLength="100" stroke-dasharray="100 100" class="{progress(pts)}"/>')
    inner.append(f'<line x1="16" y1="{y_bar}" x2="{cw - 16}" y2="{y_bar}" stroke="{GREEN}" stroke-width="2" '
                 f'stroke-linecap="round" class="{appear(s["done"], 0.4)}"/>')
    out.append(f'<g class="{appear(7.7 + i * 0.12, 0.5, 0, None, 0.35, 24)}">{"".join(inner)}</g>')
    out.append("</g>")
    return "".join(out)


def claude_window() -> str:
    x, y, w, h = 36, 74, 480, 568
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{WIN}" stroke="{WIN_EDGE}"/>',
             f'<rect x="{x}" y="{y}" width="{w}" height="30" rx="12" fill="#18181d"/>',
             f'<rect x="{x}" y="{y + 18}" width="{w}" height="12" fill="#18181d"/>',
             f'<line x1="{x}" y1="{y + 30}" x2="{x + w}" y2="{y + 30}" stroke="{WIN_EDGE}"/>']
    for k, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        parts.append(f'<circle cx="{x + 18 + k * 17}" cy="{y + 15}" r="5.2" fill="{c}" fill-opacity=".85"/>')
    parts.append(mono(x + w / 2, y + 19.5, "claude — ~/Projects/agent-hub", 11, DIM, 400, "middle"))

    tx, ty, size, lh = x + 20, y + 46, 13.0, 19.0
    L: list[Line] = []

    def add(t, spans, typing=0.0, cover=WIN):
        L.append(Line(t, spans, typing, cover))

    add(0.45, [("✻ ", CLAUDE, True), ("Claude Code", TEXT, True)])
    add(0.45, [("  ~/Projects/agent-hub", DIM)])
    add(0.45, [])
    p0 = len(L)
    add(0.9, [("› ", DIM), ("Big task: add ", TEXT), ("ahub web", BLUE), (" — a local web", TEXT)], 0.7, PROMPT_BG)
    add(1.65, [("  dashboard: live task board, task page", TEXT)], 0.7, PROMPT_BG)
    add(2.4, [("  with the live transcript, accept/reject,", TEXT)], 0.7, PROMPT_BG)
    add(3.15, [("  money panel. Delegate it through ahub.", TEXT)], 0.65, PROMPT_BG)
    add(3.9, [])
    add(4.05, [("⏺ ", TEXT), ("Four independent tasks. Workers write", TEXT)])
    add(4.05, [("  the code; I keep the decisions.", TEXT)])
    reviews = [("gemini", BLUE), ("bunny", PINK), ("gemini", BLUE), ("bunny", PINK)]
    for k, wk in enumerate(WORKERS):
        t = 4.5 + k * 0.5
        add(t, [("⏺ ", GREEN), ("Bash", TEXT, True), (f"(ahub task new --key {wk.key} …)", SOFT)], 0.28)
        add(t + 0.36, [("  ⎿ ", DIM), (wk.task, TEXT), (" queued (code, spark, review ", DIM),
                       (reviews[k][0], reviews[k][1]), ("×1)", DIM)])
    add(6.6, [])
    add(8.7, [("⏺ ", GREEN), ("Monitor", TEXT, True), ("(ahub watch)", SOFT)])
    add(8.85, [("  ⎿ ", DIM), ("waiting — 0 tokens on the code", DIM)])
    for k, wk in enumerate(WORKERS):
        d = wk.done
        add(d + 0.3, [("  ⎿ ", DIM), ("DONE ", GREEN, True), (f"{wk.task} code «{wk.short}»", SOFT),
                      (f" — ${wk.cost:.2f}", ACCENT)])
        add(d + 0.7, [("⏺ ", GREEN), ("Bash", TEXT, True), (f"(ahub accept {wk.task})", SOFT)], 0.25)
        add(d + 1.15, [("  ⎿ ", DIM), (f"{wk.task} merged into main ({SHAS[k]})", DIM)])
    # the prompt block sits behind its lines
    pb = (f'<rect x="{x + 10}" y="{ty + p0 * lh - 5}" width="{w - 20}" height="{4 * lh + 8}" rx="6" '
          f'fill="{PROMPT_BG}" class="{appear(0.85, 0.25)}"/>')
    term = Term(tx, ty, w - 40, 25, size, lh, "claude")
    parts.append(term.render(L, cursor=CLAUDE, under=pb))
    # Claude's live status line (bottom of the window), with the spinner Claude Code uses
    frames = "·✢✳✶✻✽✻✶✳✢"
    spin = []
    per = 0.11
    for k, g in enumerate(frames):
        cls = A.add([(0, "opacity:0"), (k * per, "opacity:0"), (k * per + 0.001, "opacity:1"),
                     ((k + 1) * per, "opacity:1"), ((k + 1) * per + 0.001, "opacity:0")], period=len(frames) * per)
        spin.append(f'<g class="{cls}">{mono(tx, y + h - 16, g, 13, CLAUDE)}</g>')
    status = (f'<g class="{appear(8.8, 0.4, 0, 17.9, 0.4)}">{"".join(spin)}'
              f'<text x="{tx + 18}" y="{y + h - 16}" font-family="{MONO}" font-size="12.5" xml:space="preserve" '
              f'style="white-space:pre"><tspan fill="{SOFT}">{esc("Waiting on ahub watch…")}</tspan>'
              f'<tspan fill="{DIM}">{esc("  (4 workers · Claude idle)")}</tspan></text></g>')
    parts.append(status)
    # A: big and centred; B: steps aside (transform-origin is the SVG origin)
    s = 1.12
    tx0 = (W / 2 - w * s / 2) - s * x
    ty0 = 20 - s * y
    move = A.add([(0, f"transform:translate({tx0:.1f}px,{ty0:.1f}px) scale({s})"),
                  (6.7, f"transform:translate({tx0:.1f}px,{ty0:.1f}px) scale({s})", EASE_IO),
                  (7.6, "transform:translate(0px,0px) scale(1)")])
    return f'<g class="{move}">{"".join(parts)}</g>'


def hub_and_wires(cards_xy: list[tuple[float, float, float]]) -> str:
    hx, hy = 608, 358
    out = []
    # wires
    wires_out, wires_in = [], []
    d0 = f"M516 {hy} H{hx - 36}"
    out.append(f'<path d="{d0}" stroke="{FAINT}" stroke-width="1.2" fill="none" pathLength="100" '
               f'stroke-dasharray="100 100" class="{draw(7.5, 8.0)}"/>')
    for k, (cx, cy, _) in enumerate(cards_xy):
        d = f"M{hx + 36} {hy} C{hx + 70} {hy} {cx - 40} {cy} {cx} {cy}"
        wires_out.append(d)
        out.append(f'<path d="{d}" stroke="{FAINT}" stroke-width="1.2" fill="none" pathLength="100" '
                   f'stroke-dasharray="100 100" class="{draw(7.8 + k * 0.08, 8.5 + k * 0.08)}"/>')
        wires_in.append(f"M{cx} {cy} C{cx - 40} {cy} {hx + 70} {hy} {hx + 36} {hy}")
    # dispatch: Claude → hub → each worker
    out.append(f'<path d="{d0}" stroke="{CLAUDE}" stroke-width="2.4" fill="none" pathLength="100" '
               f'stroke-dasharray="10 110" stroke-linecap="round" filter="url(#glow)" class="{pulse(8.0, 0.45)}"/>')
    for k, d in enumerate(wires_out):
        for r in range(2):
            out.append(f'<path d="{d}" stroke="{ACCENT}" stroke-width="2.4" fill="none" pathLength="100" '
                       f'stroke-dasharray="10 110" stroke-linecap="round" filter="url(#glow)" '
                       f'class="{pulse(8.4 + k * 0.1 + r * 0.55, 0.7)}"/>')
    # results: worker → hub → Claude
    rev = f"M{hx - 36} {hy} H516"
    for k, (w, d) in enumerate(zip(WORKERS, wires_in)):
        out.append(f'<path d="{d}" stroke="{GREEN}" stroke-width="2.4" fill="none" pathLength="100" '
                   f'stroke-dasharray="10 110" stroke-linecap="round" filter="url(#glow)" '
                   f'class="{pulse(w.done - 0.05, 0.5)}"/>')
        out.append(f'<path d="{rev}" stroke="{GREEN}" stroke-width="2.4" fill="none" pathLength="100" '
                   f'stroke-dasharray="10 110" stroke-linecap="round" filter="url(#glow)" '
                   f'class="{pulse(w.done + 0.42, 0.3)}"/>')
    # the hub node
    node = [f'<circle cx="{hx}" cy="{hy}" r="46" fill="url(#gHalo)"/>',
            f'<circle cx="{hx}" cy="{hy}" r="34" fill="#141016" stroke="{ACCENT}" stroke-opacity=".7" '
            f'stroke-width="1.4"/>',
            f'<circle cx="{hx}" cy="{hy}" r="34" fill="none" stroke="{ACCENT}" stroke-width="1.4" '
            f'stroke-opacity=".9" pathLength="100" stroke-dasharray="18 82" class="{A.add([(0, "stroke-dashoffset:0"), (2.4, "stroke-dashoffset:-100")], period=2.4)}"/>',
            mono(hx, hy + 9, "✻", 26, ACCENT, 400, "middle"),
            sans(hx, hy + 58, "ahub", 14, TEXT, 600, "middle"),
            mono(hx, hy + 75, "queue · gates · review", 10, DIM, 400, "middle")]
    out.append(f'<g class="{appear(7.4, 0.5)}">{"".join(node)}</g>')
    return "".join(out)


def top_bar() -> str:
    out = [f'<g class="{appear(7.6, 0.5)}">',
           mono(40, 46, "✻", 16, ACCENT),
           sans(60, 46, "agent-hub", 15, TEXT, 600),
           sans(152, 46, "Claude orchestrates · cheap models write the code", 13, DIM, 400)]
    # ticker: running · lines · money, summed from the cards
    sched = [worker_schedule(w, i) for i, w in enumerate(WORKERS)]
    ts = [WORK_START + k * 0.45 for k in range(int((16.6 - WORK_START) / 0.45) + 1)]
    vals = []
    for t in ts:
        lines = money = 0
        running = 0
        for w, s in zip(WORKERS, sched):
            f = min(1.0, max(0.0, (t - s["start"]) / (s["work_end"] - s["start"])))
            lines += int(w.added * f ** 1.15)
            money += w.cost * min(1.0, max(0.0, (t - s["start"]) / (s["done"] - s["start"])))
            running += t < s["done"]
        vals.append((t, (running, lines, money)))
    vals.append((sched[-1]["done"], (0, TOTAL_ADDED, TOTAL_COST)))

    def tick(v):
        running, lines, money = v
        state = f"{running} running" if running else "4 done"
        return (f'<text x="1160" y="46" font-family="{MONO}" font-size="13" text-anchor="end" '
                f'xml:space="preserve" style="white-space:pre"><tspan fill="{SOFT}">{esc(state)}</tspan>'
                f'<tspan fill="{FAINT}">{esc("  ·  ")}</tspan><tspan fill="{GREEN}">{esc(f"+{lines:,}")}</tspan>'
                f'<tspan fill="{DIM}">{esc(" lines")}</tspan><tspan fill="{FAINT}">{esc("  ·  ")}</tspan>'
                f'<tspan fill="{ACCENT}" font-weight="700">{esc(f"${money:.2f}")}</tspan></text>')
    out.append(discrete(vals, None, tick))
    out.append("</g>")
    return "".join(out)


def finale() -> str:
    t0 = FINALE
    x0, x1 = 170, 1030
    out = []
    out.append(f'<g class="{appear(t0, 0.6, 10)}">'
               + sans(W / 2, 132, f"THE SAME FEATURE  ·  4 TASKS  ·  +{TOTAL_ADDED:,} LINES  ·  4 REVIEWS",
                      13, DIM, 600, "middle", 'letter-spacing="2.5"') + "</g>")
    # row 1: Claude writes everything itself
    r1 = 236
    out.append(f'<g class="{appear(t0 + 0.3, 0.5, 8)}">' + sans(x0, r1, "Claude writes every line itself", 17,
                                                                 SOFT, 400) + "</g>")
    out.append(f'<line x1="{x0}" y1="{r1 + 26}" x2="{x1}" y2="{r1 + 26}" stroke="#1d1d24" stroke-width="12" '
               f'stroke-linecap="round" class="{appear(t0 + 0.3, 0.4)}"/>')
    out.append(f'<line x1="{x0}" y1="{r1 + 26}" x2="{x1}" y2="{r1 + 26}" stroke="url(#gGrey)" stroke-width="12" '
               f'stroke-linecap="round" pathLength="100" stroke-dasharray="100 100" '
               f'class="{draw(t0 + 0.45, t0 + 1.7)}"/>')
    big1 = [(t0 + 0.45 + k * 0.125, v) for k, v in enumerate((4, 13, 27, 44, 61, 79, 96, 111, 122, 130))]
    out.append(discrete(big1, None, lambda v: sans(x1, r1, f"≈ ${v}", 40, TEXT, 800, "end")))
    out.append(f'<line x1="{x1 - 150}" y1="{r1 - 13}" x2="{x1 + 4}" y2="{r1 - 13}" stroke="{RED}" stroke-width="3" '
               f'stroke-linecap="round" pathLength="100" stroke-dasharray="100 100" '
               f'class="{draw(t0 + 2.9, t0 + 3.25)}"/>')
    # row 2: Claude orchestrates through ahub
    r2 = 372
    out.append(f'<g class="{appear(t0 + 1.6, 0.5, 8)}">' + sans(x0, r2, "Claude orchestrates · workers write the code",
                                                                 17, ACCENT, 600) + "</g>")
    bar2 = x0 + max(10, (x1 - x0) * TOTAL_COST / CLAUDE_ONLY)
    out.append(f'<line x1="{x0}" y1="{r2 + 30}" x2="{x1}" y2="{r2 + 30}" stroke="#1d1d24" stroke-width="12" '
               f'stroke-linecap="round" class="{appear(t0 + 1.6, 0.4)}"/>')
    out.append(f'<line x1="{x0}" y1="{r2 + 30}" x2="{bar2:.1f}" y2="{r2 + 30}" stroke="{ACCENT}" stroke-width="12" '
               f'stroke-linecap="round" filter="url(#glow)" class="{appear(t0 + 1.9, 0.3)}"/>')
    small = [(t0 + 1.9 + k * 0.11, v) for k, v in enumerate((0.0, 0.12, 0.31, 0.52, 0.74, 0.91, TOTAL_COST))]
    out.append(discrete(small, None, lambda v: sans(x1, r2 + 8, f"${v:.2f}", 64, ACCENT, 800, "end",
                                                    'filter="url(#glowSoft)"')))
    # the punchline
    ratio = round(CLAUDE_ONLY / TOTAL_COST / 5) * 5
    pill_w = 430
    out.append(f'<g class="{appear(t0 + 3.3, 0.5, 10)}">'
               f'<rect x="{W / 2 - pill_w / 2}" y="462" width="{pill_w}" height="44" rx="22" fill="{ACCENT}" '
               f'fill-opacity=".1" stroke="{ACCENT}" stroke-opacity=".45"/>'
               + sans(W / 2, 490, f"~{ratio}× cheaper  ·  Claude's context goes to decisions", 16, TEXT, 600,
                      "middle") + "</g>")
    out.append(f'<g class="{appear(t0 + 3.8, 0.6)}">'
               + mono(W / 2 - 150, 585, "✻", 20, ACCENT)
               + sans(W / 2 - 126, 585, "agent-hub", 20, TEXT, 600)
               + f'<rect x="{W / 2 + 4}" y="564" width="150" height="30" rx="7" fill="#16161b" stroke="{WIN_EDGE}"/>'
               + mono(W / 2 + 79, 584, "pip install ahub", 13.5, SOFT, 400, "middle")
               + sans(W / 2, 628, "Workers' bill (Spark 1.3 on opencode Go) vs. the same tokens at Claude API list "
                                  "prices. Rounded, from real agent-hub runs.", 11.5, FAINT, 400, "middle")
               + "</g>")
    return f'<g class="{appear(t0, 0.01)}">{"".join(out)}</g>'


# --- fonts --------------------------------------------------------------------------------------------------

def font_face(family: str, path: Path, weight: int, chars: set[str]) -> str:
    f = TTFont(path)
    cmap = f.getBestCmap()
    keep = "".join(c for c in chars if ord(c) in cmap) or " "
    opts = subset.Options()
    opts.flavor = "woff2"
    opts.layout_features = ["kern", "liga", "calt"]
    opts.name_IDs = []
    opts.notdef_outline = False
    s = subset.Subsetter(opts)
    s.populate(text=keep)
    s.subset(f)
    f.flavor = "woff2"
    buf = io.BytesIO()
    f.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return f"@font-face{{font-family:{family};font-weight:{weight};src:url(data:font/woff2;base64,{b64}) format('woff2')}}"


def fonts() -> str:
    d = HERE / "fonts"
    chars = USED | set(" ")
    faces = [font_face("M", d / "jetbrains-mono-latin-400-normal.woff2", 400, chars),
             font_face("M", d / "jetbrains-mono-latin-700-normal.woff2", 700, chars),
             font_face("S", d / "inter-latin-400-normal.woff2", 400, chars),
             font_face("S", d / "inter-latin-600-normal.woff2", 600, chars),
             font_face("S", d / "inter-latin-800-normal.woff2", 800, chars),
             font_face("MS", d / "dejavu-sans-mono-symbols.woff2", 400, chars)]
    covered = set()
    for name in ("jetbrains-mono-latin-400-normal.woff2", "inter-latin-400-normal.woff2",
                 "dejavu-sans-mono-symbols.woff2"):
        covered |= {chr(c) for c in TTFont(d / name).getBestCmap()}
    missing = sorted(c for c in chars if c not in covered and c not in "⏺⎿")
    if missing:
        raise SystemExit(f"no embedded glyph for: {''.join(missing)!r}")
    return "".join(faces)


# --- assembly -----------------------------------------------------------------------------------------------

def build() -> str:
    cards_xy = []
    cw, ch, gap, cx, cy0 = 452, 132, 12, 708, 82
    card_svg = []
    for i, w in enumerate(WORKERS):
        y = cy0 + i * (ch + gap)
        cards_xy.append((cx, y + ch / 2, ch))
        card_svg.append(card(w, i, cx, y, cw, ch))

    stage = (f'<g class="{appear(0.0, 0.01, 0, STAGE_OUT, 0.55)}">'
             + hub_and_wires(cards_xy) + "".join(card_svg) + claude_window() + top_bar() + "</g>")
    fin = finale()
    root_fade = A.add([(0, "opacity:0"), (0.35, "opacity:1"), (T - 0.6, "opacity:1", EASE_IO), (T, "opacity:0")])

    defs = f"""<defs>
<linearGradient id="gSpark" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b5cf6"/><stop offset="1" stop-color="#06b6d4"/></linearGradient>
<linearGradient id="gBar" gradientUnits="userSpaceOnUse" x1="16" x2="436" y1="0" y2="0"><stop offset="0" stop-color="{CLAUDE}"/><stop offset="1" stop-color="{ACCENT}"/></linearGradient>
<linearGradient id="gGrey" gradientUnits="userSpaceOnUse" x1="170" x2="1030" y1="0" y2="0"><stop offset="0" stop-color="#3b3b45"/><stop offset="1" stop-color="#6b6b76"/></linearGradient>
<radialGradient id="gHalo"><stop offset="0" stop-color="{ACCENT}" stop-opacity=".28"/><stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></radialGradient>
<radialGradient id="gBg" cx=".5" cy=".42" r=".7"><stop offset="0" stop-color="#1a1210"/><stop offset=".55" stop-color="{BG}"/><stop offset="1" stop-color="#050506"/></radialGradient>
<pattern id="pDots" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#ffffff" fill-opacity=".045"/></pattern>
<filter id="glow" filterUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"><feGaussianBlur stdDeviation="3.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<filter id="glowSoft" filterUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"><feGaussianBlur stdDeviation="9" result="b"/><feColorMatrix in="b" type="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 .45 0" result="c"/><feMerge><feMergeNode in="c"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>"""
    css = fonts() + "text{font-kerning:normal}" + "".join(A.css)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'role="img" aria-label="Claude Code hands a big task to agent-hub; four cheap worker models write and '
            f'test the code in parallel, reviewers approve, Claude accepts — about $1 instead of about $130.">'
            f"<title>agent-hub: Claude orchestrates, cheap models write the code</title>"
            f"{defs}<style>{css}</style>"
            f'<rect width="{W}" height="{H}" fill="url(#gBg)"/><rect width="{W}" height="{H}" fill="url(#pDots)"/>'
            f'<g class="{root_fade}">{stage}{fin}</g></svg>')


def main() -> None:
    svg = build()
    OUT.write_text(svg, encoding="utf-8")
    print(f"{OUT.relative_to(HERE.parent.parent)}: {len(svg.encode()) / 1024:.0f} KB, {A.n} animations")


if __name__ == "__main__":
    main()
