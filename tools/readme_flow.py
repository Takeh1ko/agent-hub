"""Generate the animated README diagram: docs/assets/flow-dark.svg and flow-light.svg.

One loop of a task through the hub, drawn as a looping SVG (CSS keyframes plus one SMIL motion,
no scripts), so GitHub renders it inline in the README. Run: python tools/readme_flow.py
"""
from __future__ import annotations

from pathlib import Path

LOOP_S = 16
OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"

THEMES = {
    "dark": dict(
        bg1="#0d1020", bg2="#1a1d3a", panel="#11142b", panel_line="#2a2e52", term="#0a0c1a",
        text="#e6e8f5", dim="#8b8fb0", faint="#3a3f66", accent="#8b5cf6", cyan="#22d3ee",
        green="#34d399", red="#f87171", orange="#ff8700", box="#ffffff", box_op="0.04",
    ),
    "light": dict(
        bg1="#ffffff", bg2="#f3f1ff", panel="#ffffff", panel_line="#dcd9f2", term="#f7f7fc",
        text="#1f2240", dim="#6b6f90", faint="#c9c6e6", accent="#7c3aed", cyan="#0891b2",
        green="#059669", red="#dc2626", orange="#d75f00", box="#7c3aed", box_op="0.04",
    ),
}

MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
SANS = "-apple-system, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"

# Stage boxes: x of the left edge, title, subtitle.
BOX_W, BOX_H, BOX_Y = 120, 64, 128
STAGES = [
    (480, "queue", "slots · budgets"),
    (625, "worker", "own git worktree"),
    (770, "gates", "commit · diff · tests"),
    (915, "review", "fresh sessions"),
    (1060, "done", "event DONE"),
]
CX = [x + BOX_W // 2 for x, _, _ in STAGES]
RAIL_Y = 222


class Anim:
    """Collects keyframes; each animated element gets its own class on the shared loop clock."""

    def __init__(self) -> None:
        self.css: list[str] = []
        self.n = 0

    def add(self, frames: list[tuple[float, str]], extra: str = "") -> str:
        self.n += 1
        name = f"a{self.n}"
        body = " ".join(f"{p:g}% {{ {v} }}" for p, v in frames)
        self.css.append(f"@keyframes {name} {{ {body} }}")
        self.css.append(f".{name} {{ animation: {name} {LOOP_S}s linear infinite; {extra} }}")
        return name

    def show(self, *windows: tuple[float, float], fade: float = 0.6) -> str:
        """Visible inside each (start, end) window, in percent of the loop."""
        frames = [(0, "opacity: 0")]
        for a, b in windows:
            frames += [(a, "opacity: 0"), (a + fade, "opacity: 1"), (b, "opacity: 1"),
                       (b + fade, "opacity: 0")]
        frames.append((100, "opacity: 0"))
        return self.add(frames)

    def move_x(self, points: list[tuple[float, float]]) -> str:
        return self.add([(p, f"transform: translateX({x}px)") for p, x in points])


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(c: dict[str, str]) -> str:
    a = Anim()
    out: list[str] = []
    w = out.append

    # ---------------- terminal (the orchestrator) ----------------
    tx, ty, tw, th = 20, 20, 430, 296
    w(f'<rect x="{tx}" y="{ty}" width="{tw}" height="{th}" rx="14" fill="{c["term"]}" '
      f'stroke="{c["panel_line"]}"/>')
    for i, col in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        w(f'<circle cx="{tx + 22 + i * 18}" cy="{ty + 20}" r="5.5" fill="{col}"/>')
    w(f'<text x="{tx + tw / 2}" y="{ty + 25}" text-anchor="middle" font-family="{SANS}" '
      f'font-size="13" fill="{c["dim"]}">Claude Code · orchestrator</text>')
    w(f'<line x1="{tx}" y1="{ty + 40}" x2="{tx + tw}" y2="{ty + 40}" stroke="{c["panel_line"]}"/>')

    lx = tx + 20
    ys = [92, 118, 140, 176, 214, 240, 262, 300]
    w('<g clip-path="url(#term)" font-family="' + MONO + '" font-size="13.5">')

    def typed(y: float, text: str, t0: float, t1: float, out_at: float = 97) -> None:
        """A command line typed between t0 and t1 (a cover slides off it)."""
        width = len(text) * 8.3 + 10
        vis = a.show((t0 - 0.6, out_at))
        w(f'<text class="{vis}" x="{lx}" y="{y}" fill="{c["text"]}">'
          f'<tspan fill="{c["accent"]}">$ </tspan>{esc(text)}</text>')
        cover = a.move_x([(0, 0), (t0, 0), (t1, width), (99.9, width), (100, 0)])
        w(f'<rect class="{cover}" x="{lx + 16}" y="{y - 15}" width="{width}" height="21" '
          f'fill="{c["term"]}"/>')

    typed(ys[0], 'ahub task new "fix flaky sync test"', 1.5, 6)
    w(f'<text class="{a.show((7, 97))}" x="{lx}" y="{ys[1]}" fill="{c["text"]}">'
      f'<tspan fill="{c["orange"]}">⏺ </tspan>T42 queued · kind code · budget $0.40</text>')
    w(f'<text class="{a.show((8, 97))}" x="{lx}" y="{ys[2]}" fill="{c["dim"]}">'
      f'  ⎿ Next: ahub watch</text>')

    # waiting line with a spinner, swapped for the DONE line when the hub wakes Claude
    wait = a.show((11, 83))
    w(f'<g class="{wait}">')
    glyphs = "·✢✳✶✻✽"
    for i, g in enumerate(glyphs):
        step = 100 / len(glyphs)
        name = f"sp{i}"
        a.css.append(
            f"@keyframes {name} {{ 0% {{ opacity: 0 }} {i * step:g}% {{ opacity: 0 }} "
            f"{i * step + 0.01:g}% {{ opacity: 1 }} {(i + 1) * step:g}% {{ opacity: 1 }} "
            f"{(i + 1) * step + 0.01:g}% {{ opacity: 0 }} 100% {{ opacity: 0 }} }}"
            f" .{name} {{ animation: {name} 1.2s steps(1, end) infinite }}")
        w(f'<text class="{name}" x="{lx}" y="{ys[3]}" fill="{c["orange"]}">{g}</text>')
    w(f'<text x="{lx + 18}" y="{ys[3]}" fill="{c["dim"]}">waiting · Claude spends no tokens</text>')
    w('</g>')
    w(f'<text class="{a.show((85, 97))}" x="{lx}" y="{ys[3]}" fill="{c["text"]}">'
      f'<tspan fill="{c["green"]}" font-weight="700">DONE</tspan> T42 fix flaky sync test · 3/3</text>')

    typed(ys[4], "ahub accept T42", 88, 90.5)
    w(f'<text class="{a.show((91.5, 97))}" x="{lx}" y="{ys[5]}" fill="{c["text"]}">'
      f'<tspan fill="{c["orange"]}">⏺ </tspan>merged into main · acceptance ✓</text>')
    w(f'<text class="{a.show((92.5, 97))}" x="{lx}" y="{ys[6]}" fill="{c["dim"]}">'
      f'  ⎿ Claude’s part: two commands</text>')
    w('</g>')

    # ---------------- header ----------------
    w(f'<text x="480" y="58" font-family="{SANS}" font-size="26" font-weight="700" '
      f'fill="{c["text"]}">agent-hub</text>')
    w(f'<text x="612" y="57" font-family="{SANS}" font-size="15" fill="{c["dim"]}">'
      f'runs the work · Claude only decides</text>')

    # ---------------- stage boxes ----------------
    # connectors between boxes
    for (x, _, _), (nx, _, _) in zip(STAGES, STAGES[1:], strict=False):
        x2 = nx - 4
        w(f'<path d="M{x + BOX_W + 4} {BOX_Y + BOX_H / 2} H{x2} M{x2 - 6} {BOX_Y + BOX_H / 2 - 5} '
          f'L{x2} {BOX_Y + BOX_H / 2} L{x2 - 6} {BOX_Y + BOX_H / 2 + 5}" stroke="{c["dim"]}" '
          f'stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    # from the terminal into the queue
    w(f'<path d="M{tx + tw + 4} {BOX_Y + BOX_H / 2} H474 M468 {BOX_Y + BOX_H / 2 - 5} L474 '
      f'{BOX_Y + BOX_H / 2} L468 {BOX_Y + BOX_H / 2 + 5}" stroke="{c["dim"]}" stroke-width="1.6" '
      f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    send = a.show((6.5, 9), fade=0.3)
    sx = a.move_x([(0, 0), (6.5, 0), (9, 26), (100, 26)])
    w(f'<g class="{send}"><circle class="{sx}" cx="{tx + tw + 2}" cy="{BOX_Y + BOX_H / 2}" r="4.5" '
      f'fill="{c["cyan"]}"/></g>')

    active = {
        "queue": [(9, 18.5)],
        "worker": [(19.5, 37), (61.5, 68)],
        "gates": [(37.5, 48.5), (68.5, 73.5)],
        "review": [(49, 60.5), (74, 81.5)],
        "done": [(82, 97)],
    }
    for x, title, sub in STAGES:
        w(f'<rect x="{x}" y="{BOX_Y}" width="{BOX_W}" height="{BOX_H}" rx="12" fill="{c["box"]}" '
          f'fill-opacity="{c["box_op"]}" stroke="{c["faint"]}" stroke-width="1.4"/>')
        hl = a.show(*active[title], fade=0.8)
        color = c["green"] if title == "done" else c["accent"]
        w(f'<rect class="{hl}" x="{x}" y="{BOX_Y}" width="{BOX_W}" height="{BOX_H}" rx="12" '
          f'fill="{color}" fill-opacity="0.14" stroke="{color}" stroke-width="2.2" '
          f'filter="url(#glow)"/>')
        w(f'<text x="{x + BOX_W / 2}" y="{BOX_Y + 28}" text-anchor="middle" font-family="{SANS}" '
          f'font-size="17" font-weight="600" fill="{c["text"]}">{title}</text>')
        w(f'<text x="{x + BOX_W / 2}" y="{BOX_Y + 48}" text-anchor="middle" font-family="{SANS}" '
          f'font-size="11.5" fill="{c["dim"]}">{sub}</text>')

    # ---------------- the rail and the task token ----------------
    w(f'<line x1="480" y1="{RAIL_Y}" x2="1180" y2="{RAIL_Y}" stroke="{c["faint"]}" '
      f'stroke-width="1.4" stroke-dasharray="2 6" stroke-linecap="round"/>')
    q, wk, g, r, d = CX
    tok = a.move_x([(0, q), (18, q), (20, wk), (36.5, wk), (38.5, g), (48, g), (50, r), (60, r),
                    (62.5, wk), (67.5, wk), (69, g), (73, g), (74.5, r), (81, r), (83, d),
                    (100, d)])
    tvis = a.show((9, 97))
    w(f'<g class="{tvis}"><g class="{tok}">'
      f'<rect x="-24" y="{RAIL_Y - 12}" width="48" height="24" rx="12" fill="{c["cyan"]}"/>'
      f'<text x="0" y="{RAIL_Y + 5}" text-anchor="middle" font-family="{MONO}" font-size="13" '
      f'font-weight="700" fill="{c["bg1"]}">T42</text></g></g>')

    # ---------------- stage details ----------------
    dy = 262
    mono = f'font-family="{MONO}" font-size="12"'
    # worker: progress bar and what it does
    bx = STAGES[1][0]
    w(f'<rect x="{bx + 8}" y="{dy - 10}" width="{BOX_W - 16}" height="6" rx="3" fill="{c["faint"]}" '
      f'fill-opacity="0.6"/>')
    bar = a.add([(0, "transform: scaleX(0)"), (21, "transform: scaleX(0)"),
                 (35.5, "transform: scaleX(1)"), (61, "transform: scaleX(1)"),
                 (62, "transform: scaleX(0)"), (63, "transform: scaleX(0)"),
                 (67, "transform: scaleX(1)"), (97, "transform: scaleX(1)"),
                 (98, "transform: scaleX(0)"), (100, "transform: scaleX(0)")],
                "transform-box: fill-box; transform-origin: left center;")
    w(f'<rect class="{bar}" x="{bx + 8}" y="{dy - 10}" width="{BOX_W - 16}" height="6" rx="3" '
      f'fill="{c["accent"]}"/>')
    w(f'<text class="{a.show((24, 97))}" x="{bx + 8}" y="{dy + 14}" {mono} fill="{c["dim"]}">'
      f'✎ sync.py</text>')
    w(f'<text class="{a.show((29, 97))}" x="{bx + 8}" y="{dy + 32}" {mono} fill="{c["dim"]}">'
      f'▶ pytest -q</text>')

    # gates: three checks, set again in round 2
    gx = STAGES[2][0]
    for i, label in enumerate(("commit", "diff ⊆ paths", "tests")):
        y = dy - 4 + i * 20
        w(f'<text x="{gx + 28}" y="{y}" {mono} fill="{c["dim"]}">{esc(label)}</text>')
        w(f'<circle cx="{gx + 14}" cy="{y - 4}" r="6.5" fill="none" stroke="{c["faint"]}"/>')
        mark = a.show((40 + i * 2.5, 60.5), (69 + i * 1.2, 97), fade=0.4)
        w(f'<g class="{mark}"><circle cx="{gx + 14}" cy="{y - 4}" r="7" fill="{c["green"]}"/>'
          f'<path d="M{gx + 10.5} {y - 4} l2.4 2.6 l4.6 -5" stroke="{c["bg1"]}" stroke-width="1.8" '
          f'fill="none" stroke-linecap="round" stroke-linejoin="round"/></g>')

    # review: three reviewers; round 1 one says no, round 2 all agree
    rx = STAGES[3][0]
    for i in range(3):
        cx = rx + 24 + i * 36
        cy = dy - 2
        w(f'<circle cx="{cx}" cy="{cy}" r="12" fill="none" stroke="{c["faint"]}" stroke-width="1.4"/>')
        w(f'<text x="{cx}" y="{cy + 4}" text-anchor="middle" {mono} fill="{c["dim"]}">R{i + 1}</text>')
        first_ok = i != 1
        ok_windows = [(76 + i * 1.3, 97)]
        if first_ok:
            ok_windows.insert(0, (52 + i * 2, 60.5))
        w(f'<g class="{a.show(*ok_windows, fade=0.4)}"><circle cx="{cx}" cy="{cy}" r="12" '
          f'fill="{c["green"]}"/><path d="M{cx - 5} {cy} l3.4 3.6 l6.4 -7" stroke="{c["bg1"]}" '
          f'stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></g>')
        if not first_ok:
            w(f'<g class="{a.show((54, 60.5), fade=0.4)}"><circle cx="{cx}" cy="{cy}" r="12" '
              f'fill="{c["red"]}"/><path d="M{cx - 4.5} {cy - 4.5} l9 9 M{cx + 4.5} {cy - 4.5} '
              f'l-9 9" stroke="{c["bg1"]}" stroke-width="2" stroke-linecap="round"/></g>')
    w(f'<text class="{a.show((55.5, 60.5))}" x="{rx + 8}" y="{dy + 30}" {mono} fill="{c["red"]}">'
      f'sync.py:88 race</text>')
    w(f'<text class="{a.show((79.5, 97))}" x="{rx + 8}" y="{dy + 30}" {mono} fill="{c["green"]}">'
      f'3/3 approve</text>')

    # done
    ddx = STAGES[4][0]
    w(f'<text class="{a.show((83, 97))}" x="{ddx + BOX_W / 2}" y="{dy + 2}" text-anchor="middle" '
      f'{mono} fill="{c["green"]}">ready to accept</text>')

    # rework arc: review back to worker
    ax1, ax2 = CX[3], CX[1]
    arc = f"M{ax1} {BOX_Y - 4} C{ax1} {BOX_Y - 44} {ax2} {BOX_Y - 44} {ax2} {BOX_Y - 4}"
    w(f'<path d="{arc}" stroke="{c["faint"]}" stroke-width="1.4" fill="none" stroke-dasharray="4 5"/>')
    w(f'<path d="M{ax2 - 5} {BOX_Y - 11} L{ax2} {BOX_Y - 4} L{ax2 + 5} {BOX_Y - 11}" '
      f'stroke="{c["faint"]}" stroke-width="1.4" fill="none" stroke-linecap="round"/>')
    flow = a.add([(0, "stroke-dashoffset: 0"), (100, "stroke-dashoffset: -540")])
    rw = a.show((59.5, 67))
    w(f'<g class="{rw}"><path class="{flow}" d="{arc}" stroke="{c["red"]}" stroke-width="2.4" '
      f'fill="none" stroke-dasharray="6 6"/>'
      f'<path d="M{ax2 - 5} {BOX_Y - 11} L{ax2} {BOX_Y - 4} L{ax2 + 5} {BOX_Y - 11}" '
      f'stroke="{c["red"]}" stroke-width="2.4" fill="none" stroke-linecap="round"/></g>')
    mid = (ax1 + ax2) / 2
    w(f'<text x="{mid}" y="{BOX_Y - 38}" text-anchor="middle" font-family="{SANS}" font-size="12" '
      f'fill="{c["dim"]}">rework with notes · up to N rounds</text>')

    # ---------------- the way back: one line wakes Claude ----------------
    back = f"M{CX[4]} 290 V352 Q{CX[4]} 380 {CX[4] - 28} 380 H{tx + tw / 2 + 28} " \
           f"Q{tx + tw / 2} 380 {tx + tw / 2} {ty + th + 8}"
    w(f'<path d="{back}" stroke="{c["faint"]}" stroke-width="1.6" fill="none" '
      f'stroke-dasharray="2 6" stroke-linecap="round"/>')
    hx, hy = tx + tw / 2, ty + th + 6
    w(f'<path d="M{hx - 5} {hy + 7} L{hx} {hy} L{hx + 5} {hy + 7}" stroke="{c["dim"]}" '
      f'stroke-width="1.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>')
    lit = a.show((84, 89.5))
    w(f'<path class="{lit}" d="{back}" stroke="{c["green"]}" stroke-width="2.2" fill="none" '
      f'stroke-linecap="round" opacity="0"/>')
    t0, t1 = 0.84 * LOOP_S, 0.885 * LOOP_S
    w(f'<circle r="6" fill="{c["green"]}" opacity="0">'
      f'<animateMotion path="{back}" dur="{LOOP_S}s" repeatCount="indefinite" calcMode="linear" '
      f'keyPoints="0;0;1;1" keyTimes="0;{t0 / LOOP_S:.3f};{t1 / LOOP_S:.3f};1"/>'
      f'<animate attributeName="opacity" dur="{LOOP_S}s" repeatCount="indefinite" calcMode="discrete" '
      f'values="0;1;0" keyTimes="0;{t0 / LOOP_S:.3f};{t1 / LOOP_S:.3f}"/></circle>')
    w(f'<text x="{(CX[4] + hx) / 2 + 40}" y="404" text-anchor="middle" font-family="{SANS}" '
      f'font-size="12.5" fill="{c["dim"]}">one event line wakes Claude · ahub watch</text>')

    head = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="420" viewBox="0 0 1200 420" '
        f'role="img" aria-label="agent-hub: Claude files a task, the hub runs a worker model, '
        f'checks gates, runs a review panel with a rework round, then wakes Claude with one line '
        f'to accept">\n'
        f'<defs>\n'
        f'<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c["bg1"]}"/>'
        f'<stop offset="1" stop-color="{c["bg2"]}"/></linearGradient>\n'
        f'<clipPath id="term"><rect x="{tx}" y="{ty + 41}" width="{tw}" height="{th - 41}"/></clipPath>\n'
        f'<filter id="glow" x="-30%" y="-30%" width="160%" height="160%">'
        f'<feGaussianBlur stdDeviation="3" result="b"/><feMerge><feMergeNode in="b"/>'
        f'<feMergeNode in="SourceGraphic"/></feMerge></filter>\n'
        f'<style>\n' + "\n".join(a.css) + "\n"
        '</style>\n</defs>\n'
        '<rect width="1200" height="420" rx="20" fill="url(#bg)"/>\n'
    )
    return head + "\n".join(out) + "\n</svg>\n"


def main() -> None:
    for name, colors in THEMES.items():
        path = OUT / f"flow-{name}.svg"
        path.write_text(build(colors), encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
