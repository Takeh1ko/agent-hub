#!/usr/bin/env python3
"""Build docs/assets/agent-hub-motion.svg — the motion piece at the top of the README.

One self-contained animated SVG: CSS keyframes only (no script, so GitHub renders it through <img>), the fonts
subset and embedded as data URIs (an SVG inside <img> loads nothing external). Every animation on the main
timeline shares the loop length T, so the whole piece stays in sync on every repeat.

The story, in five scenes:
  A  Claude Code gets a big task and files four `ahub task new` commands;
  B  the window steps aside, the hub dispatches the tasks to four worker cards;
  C  the workers write the code — tool calls, test runs, line counters, the money ticker — while Claude's own
     context meter stays flat;
  D  gates pass, a reviewer model joins each card (one sends a finding back), DONE wakes Claude with one line
     (the worker's diff → one line: brief by default), Claude accepts;
  E  the bill: the workers' tokens at Claude API list prices vs. their real bill plus Claude's orchestration.
A numbered chapter line on top says what each scene is.

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

T = 24.5                      # loop length, seconds
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
                blink = A.add([(0, "opacity:1"), (ln.t + ln.typing + 0.06, "opacity:1"),
                               (ln.t + ln.typing + 0.06 + EPS, "opacity:0")])
                # the whole cover leaves once the line is typed: under a scale transform its edge would
                # otherwise shave the last glyph
                body += (f'<svg x="{self.x:.2f}" y="{top:.2f}" width="{(n + 1) * cw + 1:.2f}" height="{hgt:.2f}" '
                         f'overflow="hidden" class="{blink}"><g class="{move}">'
                         f'<rect x="0" y="1" width="{cw:.2f}" height="{hgt - 2:.2f}" fill="{cursor}"/>'
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
    review_grad: str
    added: int
    removed: int
    cost: float
    done: float
    work: list           # (text spans) tool lines, spread over the working time
    rework: list = field(default_factory=list)  # lines after a review finding (W3)


# the timeline, seconds
TYPE_AT = 0.7            # the prompt starts typing
MOVE = (5.05, 5.85)      # the Claude window steps aside
HUB_AT = 5.55            # the hub node appears
CARDS_AT = 5.85          # the worker cards slide in
DISPATCH = 6.1           # the tasks travel out
WORK_START = 6.75        # workers start
STAGE_OUT = 16.55        # the stage fades out
FINALE = 17.2            # the bill

WORKERS = [
    Worker("web-w1", "T165", "server + JSON API", "server + JSON API", "gemini", BLUE, "gGemini", 1284, 96, 0.31,
           11.55, [
        [("✎ write ", DIM), ("ahub/web/server.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/api.py", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_server.py", SOFT)],
        [("  2 failed", RED), (", 9 passed in 1.71s", DIM)],
        [("✎ edit  ", DIM), ("ahub/web/server.py", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_server.py", SOFT)],
        [("  11 passed", GREEN), (" in 1.84s", DIM)],
        [("▶ bash  ", DIM), ("git commit -m \"ahub web: server\"", SOFT)],
    ]),
    Worker("web-w2", "T166", "live task board", "task board", "bunny", PINK, "gBunny", 836, 12, 0.19, 12.45, [
        [("✎ write ", DIM), ("ahub/web/pages/board.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/board.js", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/board.css", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_board.py", SOFT)],
        [("  7 passed", GREEN), (" in 0.92s", DIM)],
        [("▶ bash  ", DIM), ("git commit -m \"board: live task board\"", SOFT)],
    ]),
    Worker("web-w3", "T167", "task page + live transcript", "task page + transcript", "gemini", BLUE, "gGemini",
           1102, 41, 0.33, 13.75, [
        [("✎ write ", DIM), ("ahub/web/pages/task.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/task.js", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_task.py", SOFT)],
        [("  9 passed", GREEN), (" in 1.12s", DIM)],
        [("▶ bash  ", DIM), ("git commit -m \"task page\"", SOFT)],
    ], rework=[
        [("✗ gemini: ", RED), ("innerHTML on model text", SOFT)],
        [("✎ edit  ", DIM), ("ahub/web/static/task.js:88", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_task.py", SOFT)],
        [("  10 passed", GREEN), (" in 1.15s", DIM)],
    ]),
    Worker("web-w4", "T168", "actions, money panel, docs", "actions + money", "bunny", PINK, "gBunny", 996, 58,
           0.21, 14.55, [
        [("✎ write ", DIM), ("ahub/web/pages/actions.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/pages/money.py", SOFT)],
        [("✎ write ", DIM), ("ahub/web/static/actions.js", SOFT)],
        [("▶ bash  ", DIM), ("pytest -q tests/test_web_actions.py", SOFT)],
        [("  12 passed", GREEN), (" in 2.03s", DIM)],
        [("✎ edit  ", DIM), ("docs/ARCHITECTURE.md, README.md", SOFT)],
        [("▶ bash  ", DIM), ("git commit -m \"actions + money\"", SOFT)],
    ]),
]
SHAS = ["4c1d9e2", "8e21f0a", "b07a3d5", "19fd6c8"]
TOTAL_ADDED = sum(w.added for w in WORKERS)
TOTAL_COST = sum(w.cost for w in WORKERS)
CLAUDE_ONLY = 80           # the workers' tokens at Claude API list prices (Opus tier), rounded
ORCHESTRATION = 0.90       # Claude's own orchestration of the four tasks, estimated (event lines, briefs, accepts)
CHAPTERS = [(0.0, "01", "You give Claude Code one big task"),
            (5.05, "02", "ahub hands the parts to cheap models"),
            (7.4, "03", "Workers write and test · reviewer models check"),
            (11.5, "04", "Claude reads one line per task — and accepts")]
JITTER = (0.0, 0.42, -0.25, 0.3, -0.12, 0.38, -0.3, 0.18, 0.05, -0.2)   # a human, uneven rhythm of tool calls


def hline(x1: float, x2: float, y: float, attrs: str) -> str:
    """A horizontal stroke as a <path> — pathLength on <line> is not reliable in Safari."""
    return f'<path d="M{x1:.1f} {y:.1f}H{x2:.1f}" fill="none" {attrs}/>'


def worker_schedule(w: Worker, i: int) -> dict:
    """Times of a worker's phases: working → checking → reviewing (→ fixing → reviewing) → done → merged."""
    start = WORK_START + i * 0.17
    s = {"start": start, "done": w.done, "merged": w.done + 1.2}
    if w.rework:
        s["check"] = w.done - 4.0
        s["review"] = w.done - 3.35
        s["fix"] = w.done - 2.6
        s["review2"] = w.done - 0.9
    else:
        s["check"] = w.done - 1.75
        s["review"] = w.done - 0.95
    s["work_end"] = s["check"] - 0.2
    return s


def avatar(cx: float, cy: float, r: float, grad: str, letter: str, ring: str = CARD) -> str:
    """A model monogram (not a logo)."""
    return (f'<circle cx="{cx}" cy="{cy}" r="{r + 2}" fill="{ring}"/><circle cx="{cx}" cy="{cy}" r="{r}" '
            f'fill="url(#{grad})"/>' + sans(cx, cy + r * 0.36, letter, r * 0.95, "#fff", 800, "middle"))


def card(w: Worker, i: int, x: float, y: float, cw: float, ch: float) -> str:
    s = worker_schedule(w, i)
    inner = [f'<rect x="0" y="0" width="{cw}" height="{ch}" rx="12" fill="{CARD}" stroke="{WIN_EDGE}" '
             f'filter="url(#shadow)"/>']
    # done: a flash of green along the edge that settles into a quiet green border
    flash = A.add([(0, "opacity:0"), (s["done"], "opacity:0", "ease-out"), (s["done"] + 0.18, "opacity:1", EASE),
                   (s["done"] + 1.1, "opacity:.5")])
    inner.append(f'<rect x="0.5" y="0.5" width="{cw - 1}" height="{ch - 1}" rx="12" fill="none" stroke="{GREEN}" '
                 f'stroke-opacity=".75" stroke-width="1.3" filter="url(#glow)" class="{flash}"/>')
    # header: the executor, the task, its title
    inner.append(avatar(26, 27, 14, "gSpark", "S"))
    inner.append(f'<text x="50" y="27" font-family="{SANS}" font-size="15.5" font-weight="600" fill="{TEXT}">'
                 f'{esc("Spark 1.3")}<tspan font-family="{MONO}" font-size="12" font-weight="400" fill="{DIM}">'
                 f'{esc(f"  {w.task}")}</tspan></text>')
    inner.append(sans(50, 47, f"ahub web: {w.title}", 13, SOFT, 400))
    # the reviewer joins when the review starts: a second monogram slides in next to the chip
    rx = cw - 118
    rev_in = A.add([(0, "opacity:0;transform:translate(-14px,0px)"),
                    (s["review"], "opacity:0;transform:translate(-14px,0px)", EASE),
                    (s["review"] + 0.45, "opacity:1;transform:translate(0px,0px)")])
    inner.append(f'<g class="{rev_in}">{avatar(rx, 23, 11, w.review_grad, w.review[0].upper())}'
                 f'{mono(rx - 16, 27, w.review, 11.5, w.review_col, 400, "end")}</g>')
    # state chip
    states = [(s["start"], "working", SOFT), (s["check"], "checking", YELLOW), (s["review"], "reviewing", BLUE)]
    if w.rework:
        states += [(s["fix"], "fixing", RED), (s["review2"], "reviewing", BLUE)]
    states += [(s["done"], "done ✓", GREEN), (s["merged"], "merged", VIOLET)]

    def chip(v):
        label, col = v
        wdt = len(label) * 7.2 + 18
        return (f'<rect x="{cw - 14 - wdt:.1f}" y="12" width="{wdt:.1f}" height="22" rx="11" fill="{col}" '
                f'fill-opacity=".12" stroke="{col}" stroke-opacity=".38"/>'
                + mono(cw - 14 - wdt / 2, 27.5, label, 12, col, 400, "middle"))
    inner.append(discrete([(t, (lb, c)) for t, lb, c in states], None, chip))
    # line counter, right of the title
    steps = 10
    vals = []
    for k in range(steps + 1):
        f = k / steps
        t = s["start"] + 0.4 + (s["work_end"] - s["start"] - 0.4) * f
        a = int(round(w.added * (f ** 1.15) / 7)) * 7 if k < steps else w.added
        r = int(round(w.removed * f)) if k < steps else w.removed
        vals.append((t, (a, r)))

    def counter(v):
        a, r = v
        return (f'<text x="{cw - 16}" y="48" font-family="{MONO}" font-size="12.5" text-anchor="end">'
                f'<tspan fill="{GREEN}">{esc(f"+{a:,}")}</tspan><tspan fill="{RED}" fill-opacity=".8">'
                f'{esc(f" −{r}")}</tspan></text>')
    inner.append(f'<g class="{appear(s["start"] + 0.4, 0.3)}">' + discrete(vals, None, counter) + "</g>")
    # the transcript, in the uneven rhythm of real tool calls
    lines: list[Line] = []
    n = len(w.work)
    gap = (s["work_end"] - s["start"] - 0.3) / max(1, n - 1)
    for k, sp in enumerate(w.work):
        j = JITTER[(k + i * 3) % len(JITTER)] * gap if 0 < k < n - 1 else 0
        lines.append(Line(s["start"] + 0.3 + gap * k + j, sp, cover=CARD))
    lines.append(Line(s["check"] + 0.15, [("✓ gates ", GREEN), ("commit · diff ⊆ paths · tests", DIM)], cover=CARD))
    if w.rework:
        lines.append(Line(s["review"] + 0.15, [("◆ review ", DIM), (w.review, w.review_col),
                                               (" · fresh session", DIM)], cover=CARD))
        lines.append(Line(s["fix"] - 0.05, w.rework[0], cover=CARD))
        for k, sp in enumerate(w.rework[1:]):
            lines.append(Line(s["fix"] + 0.4 + k * 0.45, sp, cover=CARD))
        lines.append(Line(s["review2"] + 0.1, [("◆ review ", DIM), (w.review, w.review_col), (" · round 2", DIM)],
                          cover=CARD))
    else:
        lines.append(Line(s["review"] + 0.15, [("◆ review ", DIM), (w.review, w.review_col),
                                               (" · fresh session", DIM)], cover=CARD))
    lines.append(Line(s["done"] - 0.1, [("✓ approve", GREEN), (" · no findings", DIM)], cover=CARD))
    inner.append(Term(16, 62, cw - 32, 3, 12.5, 17.5, f"w{i}").render(lines))
    # progress
    pts = [(s["start"] + 0.3, 0.04), (s["work_end"], 0.72), (s["check"] + 0.6, 0.82), (s["done"], 1.0)]
    if w.rework:
        pts = [(s["start"] + 0.3, 0.04), (s["work_end"], 0.66), (s["review"], 0.74), (s["fix"] + 1.2, 0.86),
               (s["done"], 1.0)]
    yb = ch - 10
    inner.append(hline(16, cw - 16, yb, 'stroke="#1f1f26" stroke-width="2.5" stroke-linecap="round"'))
    inner.append(hline(16, cw - 16, yb, f'stroke="{ACCENT}" stroke-width="2.5" stroke-linecap="round" '
                                        f'pathLength="100" stroke-dasharray="100 100" class="{progress(pts)}"'))
    inner.append(hline(16, cw - 16, yb, f'stroke="{GREEN}" stroke-width="2.5" stroke-linecap="round" '
                                        f'class="{appear(s["done"], 0.4)}"'))
    slide = appear(CARDS_AT + i * 0.1, 0.55, 0, None, 0.35, 28)
    return f'<g transform="translate({x},{y})"><g class="{slide}">{"".join(inner)}</g></g>'


def claude_window() -> str:
    x, y, w, h = 32, 72, 500, 576
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{WIN}" stroke="{WIN_EDGE}" '
             f'filter="url(#shadow)"/>',
             f'<path d="M{x} {y + 31}V{y + 12}a12 12 0 0 1 12-12H{x + w - 12}a12 12 0 0 1 12 12V{y + 31}Z" '
             f'fill="#18181d"/>',
             hline(x, x + w, y + 31, f'stroke="{WIN_EDGE}"')]
    for k, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        parts.append(f'<circle cx="{x + 19 + k * 18}" cy="{y + 15.5}" r="5.5" fill="{c}" fill-opacity=".85"/>')
    parts.append(mono(x + w / 2, y + 20, "claude — ~/Projects/agent-hub", 11.5, DIM, 400, "middle"))

    tx, ty, size, lh = x + 20, y + 48, 13.6, 20.0
    L: list[Line] = []

    def add(t, spans, typing=0.0, cover=WIN):
        L.append(Line(t, spans, typing, cover))

    add(0.35, [("✻ ", CLAUDE, True), ("Claude Code", TEXT, True)])
    add(0.35, [("  ~/Projects/agent-hub", DIM)])
    add(0.35, [])
    p0 = len(L)
    t = TYPE_AT
    for k, (sp, dur) in enumerate((
            ([("› ", DIM), ("Big task: add ", TEXT), ("ahub web", BLUE), (" — a local web", TEXT)], 0.5),
            ([("  dashboard: live task board, task page", TEXT)], 0.48),
            ([("  with the live transcript, accept/reject,", TEXT)], 0.48),
            ([("  money panel. Delegate it through ahub.", TEXT)], 0.45))):
        add(t, sp, dur, PROMPT_BG)
        t += dur + 0.08
    add(t + 0.25, [])
    add(t + 0.35, [("⏺ ", TEXT), ("Four independent tasks. Workers write", TEXT)])
    add(t + 0.35, [("  the code; I keep the decisions.", TEXT)])
    t0 = t + 0.7
    for k, wk in enumerate(WORKERS):
        tk = t0 + k * 0.36
        add(tk, [("⏺ ", GREEN), ("Bash", TEXT, True), (f"(ahub task new --key {wk.key} …)", SOFT)], 0.2)
        add(tk + 0.27, [("  ⎿ ", DIM), (wk.task, TEXT), (" queued (code, spark, review ", DIM),
                        (wk.review, wk.review_col), ("×1)", DIM)])
    add(MOVE[0] + 0.2, [])
    add(WORK_START + 0.1, [("⏺ ", GREEN), ("Monitor", TEXT, True), ("(ahub watch)", SOFT)])
    add(WORK_START + 0.25, [("  ⎿ ", DIM), ("waiting — 0 tokens on the code", DIM)])
    for k, wk in enumerate(WORKERS):
        d = wk.done
        add(d + 0.3, [("  ⎿ ", DIM), ("DONE ", GREEN, True), (f"{wk.task} code «{wk.short}»", SOFT),
                      (f" — ${wk.cost:.2f}", ACCENT)])
        add(d + 0.68, [("⏺ ", GREEN), ("Bash", TEXT, True), (f"(ahub accept {wk.task})", SOFT)], 0.22)
        add(d + 1.1, [("  ⎿ ", DIM), (f"{wk.task} merged into main ({SHAS[k]})", DIM)])
    # the prompt block sits behind its lines and scrolls with them
    pb = (f'<rect x="{x + 10}" y="{ty + p0 * lh - 5}" width="{w - 20}" height="{4 * lh + 8}" rx="7" '
          f'fill="{PROMPT_BG}" class="{appear(TYPE_AT - 0.05, 0.25)}"/>')
    rows = 21
    parts.append(Term(tx, ty, w - 40, rows, size, lh, "claude").render(L, cursor=CLAUDE, under=pb))
    # the footer — brief by default: what reached Claude (event lines) against what the workers wrote,
    # then Claude Code's spinner while it waits and its context, flat while the workers write
    y1, yb = y + h - 37, y + h - 14
    parts.append(hline(x + 1, x + w - 1, y + h - 58, f'stroke="{WIN_EDGE}" class="{appear(WORK_START, 0.4)}"'))
    read = [(WORK_START, (0, 0))]
    n = size_b = 0
    for t, add_b in [(WORK_START + 0.25, 31)] + [e for wk in WORKERS for e in ((wk.done + 0.3, 58), (wk.done + 1.1, 36))]:
        n += 1
        size_b += add_b
        read.append((t, (n, size_b)))
    sched = [worker_schedule(wk, i) for i, wk in enumerate(WORKERS)]
    wrote = []
    for k in range(int((WORKERS[-1].done - WORK_START) / 0.4) + 1):
        t = WORK_START + k * 0.4
        wrote.append((t, sum(int(wk.added * min(1.0, max(0.0, (t - sc["start"]) / (sc["work_end"] - sc["start"])))
                                 ** 1.15) for wk, sc in zip(WORKERS, sched))))
    wrote.append((WORKERS[-1].done, TOTAL_ADDED))

    def read_txt(v):
        n, size_b = v
        return (f'<text x="{tx}" y="{y1}" font-family="{MONO}" font-size="12.5" xml:space="preserve" '
                f'style="white-space:pre"><tspan fill="{DIM}">{esc("Claude read ")}</tspan>'
                f'<tspan fill="{CLAUDE}" font-weight="700">{esc(f"{n} line" + ("" if n == 1 else "s"))}</tspan>'
                f'<tspan fill="{DIM}">{esc(f" · {size_b} B")}</tspan></text>')

    def wrote_txt(v):
        return (f'<text x="{x + w - 20}" y="{y1}" font-family="{MONO}" font-size="12.5" text-anchor="end" '
                f'xml:space="preserve" style="white-space:pre"><tspan fill="{DIM}">{esc("workers wrote ")}</tspan>'
                f'<tspan fill="{GREEN}" font-weight="700">{esc(f"+{v:,} lines")}</tspan></text>')
    parts.append(f'<g class="{appear(WORK_START, 0.4)}">{discrete(read, None, read_txt)}'
                 f'{discrete(wrote, None, wrote_txt)}</g>')
    frames = "·✢✳✶✻✽✻✶✳✢"
    per = 0.11
    spin = []
    for k, g in enumerate(frames):
        cls = A.add([(0, "opacity:0"), (k * per, "opacity:0"), (k * per + 0.001, "opacity:1"),
                     ((k + 1) * per, "opacity:1"), ((k + 1) * per + 0.001, "opacity:0")], period=len(frames) * per)
        spin.append(f'<g class="{cls}">{mono(tx, yb, g, 13.5, CLAUDE)}</g>')
    last = WORKERS[-1].done + 1.15
    parts.append(f'<g class="{appear(WORK_START + 0.1, 0.4, 0, last, 0.3)}">{"".join(spin)}'
                 f'{mono(tx + 18, yb, "Waiting on ahub watch…", 13, SOFT)}</g>')
    parts.append(f'<g class="{appear(last + 0.05, 0.3)}">{mono(tx, yb, "✻", 13.5, GREEN)}'
                 f'{mono(tx + 18, yb, "4 merged · 0 lines by Claude", 13, SOFT)}</g>')
    ctx = [(WORK_START, 5)] + [(wk.done + 0.35, 5 + (k + 1) * 0.5) for k, wk in enumerate(WORKERS)]
    mx = x + w - 20

    def meter(v):
        fill = 70 * v / 100
        return (mono(mx - 78, yb, f"{v:g}%", 12.5, TEXT, 700, "end")
                + f'<rect x="{mx - 72}" y="{yb - 8}" width="72" height="7" rx="3.5" fill="#24242c"/>'
                + f'<rect x="{mx - 72}" y="{yb - 8}" width="{max(7, fill):.1f}" height="7" rx="3.5" fill="{CLAUDE}"/>')
    parts.append(f'<g class="{appear(WORK_START + 0.1, 0.4)}">'
                 + mono(mx - 118, yb, "context", 12, DIM, 400, "end")
                 + discrete(ctx, None, meter) + "</g>")
    # A: big and centred; B: steps aside (transform-origin is the SVG origin)
    s = 1.04
    tx0 = (W / 2 - w * s / 2) - s * x
    ty0 = 62 - s * y
    move = A.add([(0, f"transform:translate({tx0:.1f}px,{ty0:.1f}px) scale({s})"),
                  (MOVE[0], f"transform:translate({tx0:.1f}px,{ty0:.1f}px) scale({s})", EASE_IO),
                  (MOVE[1], "transform:translate(0px,0px) scale(1)")])
    return f'<g class="{move}">{"".join(parts)}</g>'


def hub_and_wires(cards_xy: list[tuple[float, float, float]]) -> str:
    hx, hy = 622, 362
    wx = 532                  # the Claude window's right edge
    out = []
    wires_out, wires_in = [], []
    d0 = f"M{wx} {hy}H{hx - 38}"
    out.append(f'<path d="{d0}" stroke="{FAINT}" stroke-width="1.2" fill="none" pathLength="100" '
               f'stroke-dasharray="100 100" class="{draw(HUB_AT + 0.1, HUB_AT + 0.5)}"/>')
    for k, (cx, cy, _) in enumerate(cards_xy):
        d = f"M{hx + 38} {hy}C{hx + 72} {hy} {cx - 42} {cy} {cx} {cy}"
        wires_out.append(d)
        out.append(f'<path d="{d}" stroke="{FAINT}" stroke-width="1.2" fill="none" pathLength="100" '
                   f'stroke-dasharray="100 100" class="{draw(HUB_AT + 0.3 + k * 0.07, HUB_AT + 0.9 + k * 0.07)}"/>')
        wires_in.append(f"M{cx} {cy}C{cx - 42} {cy} {hx + 72} {hy} {hx + 38} {hy}")
    glow = 'fill="none" pathLength="100" stroke-dasharray="10 110" stroke-linecap="round" filter="url(#glow)"'
    out.append(f'<path d="{d0}" stroke="{CLAUDE}" stroke-width="2.6" {glow} class="{pulse(DISPATCH, 0.4)}"/>')
    for k, d in enumerate(wires_out):
        for r in range(2):
            out.append(f'<path d="{d}" stroke="{ACCENT}" stroke-width="2.6" {glow} '
                       f'class="{pulse(DISPATCH + 0.35 + k * 0.09 + r * 0.5, 0.65)}"/>')
    rev = f"M{hx - 38} {hy}H{wx}"
    for wk, d in zip(WORKERS, wires_in):
        out.append(f'<path d="{d}" stroke="{GREEN}" stroke-width="2.6" {glow} class="{pulse(wk.done - 0.05, 0.5)}"/>')
        out.append(f'<path d="{rev}" stroke="{GREEN}" stroke-width="2.6" {glow} class="{pulse(wk.done + 0.42, 0.3)}"/>')
    # the hub node: a slow orbit while anything runs, a beat on every DONE
    beat = [(0, "transform:scale(1)")]
    for wk in WORKERS:
        beat += [(wk.done + 0.35, "transform:scale(1)", "ease-out"), (wk.done + 0.5, "transform:scale(1.08)", EASE),
                 (wk.done + 0.9, "transform:scale(1)")]
    beat_cls = A.add(beat)
    orbit = A.add([(0, "stroke-dashoffset:0"), (2.4, "stroke-dashoffset:-100")], period=2.4)
    node = [f'<circle cx="{hx}" cy="{hy}" r="54" fill="url(#gHalo)"/>',
            f'<g style="transform-origin:{hx}px {hy}px" class="{beat_cls}">',
            f'<circle cx="{hx}" cy="{hy}" r="36" fill="#141016" stroke="{ACCENT}" stroke-opacity=".55" '
            f'stroke-width="1.4"/>',
            f'<circle cx="{hx}" cy="{hy}" r="36" fill="none" stroke="{ACCENT}" stroke-width="1.8" '
            f'stroke-linecap="round" pathLength="100" stroke-dasharray="16 84" class="{orbit}"/>',
            mono(hx, hy + 10, "✻", 28, ACCENT, 400, "middle"), "</g>",
            sans(hx, hy + 62, "ahub", 15, TEXT, 600, "middle"),
            mono(hx, hy + 80, "queue · gates · review", 10.5, DIM, 400, "middle")]
    out.append(f'<g class="{appear(HUB_AT, 0.5)}">{"".join(node)}</g>')
    return "".join(out)


def chapter_row(num: str, title: str) -> str:
    return (mono(176, 44, num, 14, ACCENT, 700)
            + sans(202, 44, title, 15.5, TEXT, 600))


def top_bar() -> str:
    out = [mono(36, 44, "✻", 17, ACCENT), sans(58, 44, "agent-hub", 16, TEXT, 600),
           f'<path d="M158 30V50" stroke="{FAINT}"/>']
    # chapters: each slides up into place, the previous one leaves upward
    for k, (t, num, title) in enumerate(CHAPTERS):
        t_out = CHAPTERS[k + 1][0] - 0.3 if k + 1 < len(CHAPTERS) else None
        out.append(f'<g class="{appear(t + 0.15, 0.45, 8, t_out, 0.3)}">{chapter_row(num, title)}</g>')
    sched = [worker_schedule(wk, i) for i, wk in enumerate(WORKERS)]
    end = sched[-1]["done"]
    ts = [WORK_START + k * 0.4 for k in range(int((end - WORK_START) / 0.4) + 1)]
    vals = []
    for t in ts:
        lines = money = running = 0
        for wk, s in zip(WORKERS, sched):
            f = min(1.0, max(0.0, (t - s["start"]) / (s["work_end"] - s["start"])))
            lines += int(wk.added * f ** 1.15)
            money += wk.cost * min(1.0, max(0.0, (t - s["start"]) / (s["done"] - s["start"])))
            running += t < s["done"]
        vals.append((t, (running, lines, money)))
    vals.append((end, (0, TOTAL_ADDED, TOTAL_COST)))

    def tick(v):
        running, lines, money = v
        state = f"{running} running" if running else "4 done"
        return (f'<text x="1164" y="44" font-family="{MONO}" font-size="13.5" text-anchor="end" '
                f'xml:space="preserve" style="white-space:pre"><tspan fill="{SOFT}">{esc(state)}</tspan>'
                f'<tspan fill="{FAINT}">{esc("  ·  ")}</tspan><tspan fill="{DIM}">{esc("workers ")}</tspan>'
                f'<tspan fill="{ACCENT}" font-weight="700">{esc(f"${money:.2f}")}</tspan></text>')
    out.append(f'<g class="{appear(CARDS_AT, 0.5)}">{discrete(vals, None, tick)}</g>')
    return "".join(out)


def handoff() -> str:
    """At every DONE: the worker's whole diff becomes one line for Claude — brief by default."""
    out = []
    for k, wk in enumerate(WORKERS):
        t_out = WORKERS[k + 1].done - 0.05 if k + 1 < len(WORKERS) else wk.done + 1.6
        pill = (f'<rect x="548" y="262" width="148" height="42" rx="10" fill="#0d1a12" stroke="{GREEN}" '
                f'stroke-opacity=".45"/>'
                + mono(622, 279, f"+{wk.added:,} lines", 12.5, GREEN, 700, "middle")
                + mono(622, 296, "→ 1 line to Claude", 11.5, SOFT, 400, "middle"))
        out.append(f'<g class="{appear(wk.done + 0.05, 0.3, 6, t_out, 0.2)}">{pill}</g>')
    return "".join(out)


def finale() -> str:
    t0 = FINALE
    x0, x1 = 170, 1030
    total = TOTAL_COST + ORCHESTRATION
    out = [mono(36, 44, "✻", 17, ACCENT), sans(58, 44, "agent-hub", 16, TEXT, 600),
           f'<path d="M158 30V50" stroke="{FAINT}"/>', chapter_row("05", "The bill")]
    out.append(f'<g class="{appear(t0 + 0.1, 0.6, 10)}">'
               + sans(W / 2, 138, f"THE SAME FEATURE  ·  4 TASKS  ·  +{TOTAL_ADDED:,} LINES  ·  4 REVIEWS PASSED",
                      13, DIM, 600, "middle", 'letter-spacing="2.5"') + "</g>")
    # row 1: Claude writes and reviews everything itself
    r1 = 232
    out.append(f'<g class="{appear(t0 + 0.3, 0.5, 8)}">'
               + sans(x0, r1, "Claude writes and reviews every line itself", 18, SOFT, 400) + "</g>")
    out.append(hline(x0, x1, r1 + 26, f'stroke="#1d1d24" stroke-width="12" stroke-linecap="round" '
                                      f'class="{appear(t0 + 0.3, 0.4)}"'))
    out.append(hline(x0, x1, r1 + 26, f'stroke="url(#gGrey)" stroke-width="12" stroke-linecap="round" '
                                      f'pathLength="100" stroke-dasharray="100 100" class="{draw(t0 + 0.45, t0 + 1.6)}"'))
    big1 = [(t0 + 0.45 + k * 0.12, v) for k, v in enumerate((3, 9, 18, 29, 41, 53, 64, 73, 78, CLAUDE_ONLY))]
    dim1 = A.add([(0, f"fill:{TEXT}"), (t0 + 2.9, f"fill:{TEXT}", EASE), (t0 + 3.4, "fill:#6f6f79")])
    out.append(f'<g class="{dim1}">'
               + discrete(big1, None, lambda v: sans(x1, r1, f"≈ ${v}", 42, "inherit", 800, "end")) + "</g>")
    out.append(hline(x1 - 132, x1 + 4, r1 - 14, f'stroke="{RED}" stroke-width="3" stroke-linecap="round" '
                                                f'pathLength="100" stroke-dasharray="100 100" '
                                                f'class="{draw(t0 + 2.9, t0 + 3.25)}"'))
    # row 2: Claude orchestrates, cheap models write; the bar is two parts — workers, then Claude itself
    r2 = 360
    out.append(f'<g class="{appear(t0 + 1.5, 0.5, 8)}">'
               + sans(x0, r2, "Claude orchestrates · cheap models write the code", 18, ACCENT, 600) + "</g>")
    unit = (x1 - x0) / CLAUDE_ONLY
    xa = x0 + max(8, TOTAL_COST * unit)
    xb = xa + max(8, ORCHESTRATION * unit)
    out.append(hline(x0, x1, r2 + 30, f'stroke="#1d1d24" stroke-width="12" stroke-linecap="round" '
                                      f'class="{appear(t0 + 1.5, 0.4)}"'))
    out.append(hline(x0, xb, r2 + 30, f'stroke="{CLAUDE}" stroke-width="12" stroke-linecap="round" '
                                      f'filter="url(#glow)" class="{appear(t0 + 2.1, 0.3)}"'))
    out.append(hline(x0, xa, r2 + 30, f'stroke="{ACCENT}" stroke-width="12" stroke-linecap="round" '
                                      f'class="{appear(t0 + 1.8, 0.3)}"'))
    small = [(t0 + 1.8 + k * 0.1, f"${v:.2f}") for k, v in enumerate((0.0, 0.35, 0.71, 1.04, 1.38, 1.71))]
    small.append((t0 + 2.5, f"≈ ${total:.0f}"))
    out.append(discrete(small, None, lambda v: sans(x1, r2 + 8, v, 66, ACCENT, 800, "end",
                                                    'filter="url(#glowSoft)"')))
    # what the right-hand bill is made of
    legend = (f'<circle cx="{x0 + 5}" cy="{r2 + 61}" r="5" fill="{ACCENT}"/>'
              + f'<text x="{x0 + 17}" y="{r2 + 66}" font-family="{SANS}" font-size="14" xml:space="preserve">'
              f'<tspan fill="{SOFT}">{esc("workers write, test and review")}</tspan>'
              f'<tspan fill="{ACCENT}" font-weight="600">{esc(f"  ${TOTAL_COST:.2f}")}</tspan></text>'
              + f'<circle cx="{x0 + 345}" cy="{r2 + 61}" r="5" fill="{CLAUDE}"/>'
              + f'<text x="{x0 + 357}" y="{r2 + 66}" font-family="{SANS}" font-size="14" xml:space="preserve">'
              f'<tspan fill="{SOFT}">{esc("Claude: one line per task, decisions only")}</tspan>'
              f'<tspan fill="{CLAUDE}" font-weight="600">{esc(f"  ≈ ${ORCHESTRATION:.2f}")}</tspan></text>')
    out.append(f'<g class="{appear(t0 + 2.3, 0.4, 6)}">{legend}</g>')
    # the punchline
    ratio = round(CLAUDE_ONLY / total / 5) * 5
    pill_w = 470
    out.append(f'<g class="{appear(t0 + 3.3, 0.5, 10)}">'
               f'<rect x="{W / 2 - pill_w / 2}" y="474" width="{pill_w}" height="46" rx="23" fill="{ACCENT}" '
               f'fill-opacity=".1" stroke="{ACCENT}" stroke-opacity=".45"/>'
               + sans(W / 2, 503, f"~{ratio}× cheaper  ·  brief answers keep Claude's context free", 16.5, TEXT, 600,
                      "middle") + "</g>")
    out.append(f'<g class="{appear(t0 + 3.8, 0.6)}">'
               + mono(W / 2 - 152, 590, "✻", 20, ACCENT)
               + sans(W / 2 - 128, 590, "agent-hub", 20, TEXT, 600)
               + f'<rect x="{W / 2 + 2}" y="568" width="156" height="31" rx="7" fill="#16161b" stroke="{WIN_EDGE}"/>'
               + mono(W / 2 + 80, 588.5, "pip install ahub", 14, SOFT, 400, "middle")
               + sans(W / 2, 634, "Top: the workers' tokens at Claude API list prices. Bottom: their real bill "
                                  "(Spark 1.3 on opencode Go) plus Claude's orchestration, estimated. Rounded.",
                      11.5, FAINT, 400, "middle")
               + "</g>")
    return f'<g class="{appear(t0, 0.5)}">{"".join(out)}</g>'


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
    cw, ch, gap, cx, cy0 = 456, 134, 12, 712, 76
    card_svg = []
    for i, w in enumerate(WORKERS):
        y = cy0 + i * (ch + gap)
        cards_xy.append((cx, y + ch / 2, ch))
        card_svg.append(card(w, i, cx, y, cw, ch))

    stage = (f'<g class="{appear(0.0, 0.01, 0, STAGE_OUT, 0.55)}">'
             + hub_and_wires(cards_xy) + handoff() + "".join(card_svg) + claude_window() + top_bar() + "</g>")
    fin = finale()
    root_fade = A.add([(0, "opacity:0"), (0.35, "opacity:1"), (T - 0.6, "opacity:1", EASE_IO), (T, "opacity:0")])

    defs = f"""<defs>
<linearGradient id="gSpark" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#8b5cf6"/><stop offset="1" stop-color="#06b6d4"/></linearGradient>
<linearGradient id="gGemini" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#3b82f6"/><stop offset="1" stop-color="#a855f7"/></linearGradient>
<linearGradient id="gBunny" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#f472b6"/><stop offset="1" stop-color="#fb923c"/></linearGradient>
<clipPath id="cRoot"><rect width="{W}" height="{H}" rx="18"/></clipPath>
<filter id="shadow" filterUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"><feGaussianBlur in="SourceAlpha" stdDeviation="10"/><feOffset dy="8" result="o"/><feFlood flood-color="#000" flood-opacity=".55"/><feComposite in2="o" operator="in" result="s"/><feMerge><feMergeNode in="s"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<linearGradient id="gGrey" gradientUnits="userSpaceOnUse" x1="170" x2="1030" y1="0" y2="0"><stop offset="0" stop-color="#3b3b45"/><stop offset="1" stop-color="#6b6b76"/></linearGradient>
<radialGradient id="gHalo"><stop offset="0" stop-color="{ACCENT}" stop-opacity=".28"/><stop offset="1" stop-color="{ACCENT}" stop-opacity="0"/></radialGradient>
<radialGradient id="gBg" cx=".5" cy=".42" r=".7"><stop offset="0" stop-color="#1a1210"/><stop offset=".55" stop-color="{BG}"/><stop offset="1" stop-color="#050506"/></radialGradient>
<pattern id="pDots" width="24" height="24" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#ffffff" fill-opacity=".045"/></pattern>
<filter id="glow" filterUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"><feGaussianBlur stdDeviation="3.2" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
<filter id="glowSoft" filterUnits="userSpaceOnUse" x="0" y="0" width="{W}" height="{H}"><feGaussianBlur stdDeviation="9" result="b"/><feColorMatrix in="b" type="matrix" values="1 0 0 0 0  0 1 0 0 0  0 0 1 0 0  0 0 0 .45 0" result="c"/><feMerge><feMergeNode in="c"/><feMergeNode in="SourceGraphic"/></feMerge></filter>
</defs>"""
    still = WORKERS[-1].done + 1.6
    css = (fonts() + "text{font-kerning:normal;text-rendering:geometricPrecision}" + "".join(A.css)
           + f"@media (prefers-reduced-motion:reduce){{*{{animation-play-state:paused!important;"
             f"animation-delay:-{still:.2f}s!important}}}}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
            f'role="img" aria-label="Claude Code hands a big task to agent-hub; four cheap worker models write and '
            f'test the code in parallel, reviewers approve, Claude reads one line per task and accepts — about $2 instead of about $80.">'
            f"<title>agent-hub: Claude orchestrates, cheap models write the code</title>"
            f"{defs}<style>{css}</style>"
            f'<g clip-path="url(#cRoot)"><rect width="{W}" height="{H}" fill="url(#gBg)"/>'
            f'<rect width="{W}" height="{H}" fill="url(#pDots)"/>'
            f'<g class="{root_fade}">{stage}{fin}</g></g>'
            f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="17.5" fill="none" stroke="#ffffff" '
            f'stroke-opacity=".08"/></svg>')


def main() -> None:
    svg = build()
    OUT.write_text(svg, encoding="utf-8")
    print(f"{OUT.relative_to(HERE.parent.parent)}: {len(svg.encode()) / 1024:.0f} KB, {A.n} animations")


if __name__ == "__main__":
    main()
