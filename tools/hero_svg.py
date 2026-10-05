"""Generate docs/assets/hero.svg — the animated README header.

The picture is an abstract loop of one task through the hub, no code on screen:
the orchestrator (warm) files a task and goes quiet; the hub (the brand mark)
hands it to cheap worker models (cyan) that work in parallel; the results come
back through the gates (three rings) and a review panel of fresh sessions; one
thin line wakes the orchestrator, it decides, and the hub merges.

Every timed animation is SMIL with the same duration and keyTimes inside one
loop, so the whole story stays in sync and repeats forever inside an <img> on
GitHub (no scripts). Ambient motion (stars, aurora, the spinning mark) is CSS.

Run: python3 tools/hero_svg.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

W, H = 1280, 540
T = 14.0  # the loop, seconds

O = (210.0, 200.0)  # orchestrator
HUB = (640.0, 200.0)
WORKERS = [(1015, 85), (1125, 135), (1045, 205), (1135, 272), (1010, 318)]
ACTIVE = (1, 3)  # the workers that get a task in this loop
REVIEW_R = 145
REVIEWERS = [
    (HUB[0] + REVIEW_R * math.cos(math.radians(a)), HUB[1] + REVIEW_R * math.sin(math.radians(a)))
    for a in (-130, -90, -50)
]
GATES_R = (78, 90, 102)

WARM, WARM2 = "#ffb86b", "#ff7a59"
VIOLET, CYAN, MINT, GOLD = "#8b5cf6", "#22d3ee", "#34d399", "#ffd27a"

SANS = "'Inter','Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,'SFMono-Regular','JetBrains Mono',Menlo,Consolas,monospace"

EASE = ".45 0 .25 1"
LIN = "0 0 1 1"


def f(x: float) -> str:
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def anim(attr: str, frames: list[tuple[float, object]], spline: bool = False) -> str:
    """One SMIL <animate> over the shared loop; frames are (time 0..1, value)."""
    if frames[0][0] > 0:
        frames = [(0.0, frames[0][1])] + frames
    if frames[-1][0] < 1:
        frames = frames + [(1.0, frames[-1][1])]
    times = [t for t, _ in frames]
    assert times == sorted(times) and 0 <= times[0] and times[-1] <= 1, (attr, times)
    kt = ";".join(f(t) for t, _ in frames)
    vals = ";".join(str(v) for _, v in frames)
    extra = ""
    if spline:
        extra = f' calcMode="spline" keySplines="{";".join([EASE] * (len(frames) - 1))}"'
    return (f'<animate attributeName="{attr}" dur="{T}s" repeatCount="indefinite" '
            f'keyTimes="{kt}" values="{vals}"{extra}/>')


def window(t0: float, t1: float, fade: float = 0.012, peak: float = 1.0) -> list[tuple[float, object]]:
    """Opacity frames: invisible, fade in at t0, hold, fade out by t1."""
    return [(0, 0), (t0, 0), (t0 + fade, peak), (t1 - fade, peak), (t1, 0), (1, 0)]


def motion(path_id: str, t0: float, t1: float, reverse: bool = False) -> str:
    a, b = ("1", "0") if reverse else ("0", "1")
    return (f'<animateMotion dur="{T}s" repeatCount="indefinite" calcMode="spline" '
            f'keyPoints="{a};{a};{b};{b}" keyTimes="0;{f(t0)};{f(t1)};1" '
            f'keySplines="{LIN};{EASE};{LIN}"><mpath href="#{path_id}" xlink:href="#{path_id}"/></animateMotion>')


def comet(path_id: str, t0: float, t1: float, color: str, glow: str, reverse: bool = False,
          size: float = 1.0) -> str:
    """A bright head with a fading tail travelling along a path inside [t0, t1]."""
    out = []
    lag = 0.0055
    for i in range(5, -1, -1):
        d = i * lag
        r = (4.2 - i * 0.55) * size
        op = 1.0 - i * 0.16
        a, b = t0 + d, t1 + d
        if b >= 0.999:
            a, b = a - d, b - d
        body = f'<circle r="{f(r)}" fill="{color}" opacity="0">'
        body += anim("opacity", window(a, b, 0.008, op))
        body += motion(path_id, a, b, reverse) + "</circle>"
        out.append(body)
    halo = f'<circle r="{f(16 * size)}" fill="url(#{glow})" opacity="0">'
    halo += anim("opacity", window(t0, t1, 0.01)) + motion(path_id, t0, t1, reverse) + "</circle>"
    out.append(halo)
    return "\n".join(out)


def cubic(a: tuple[float, float], b: tuple[float, float], k: float = 0.42) -> str:
    dx = b[0] - a[0]
    return (f"M{f(a[0])} {f(a[1])} C{f(a[0] + dx * k)} {f(a[1])} "
            f"{f(b[0] - dx * k)} {f(b[1])} {f(b[0])} {f(b[1])}")


def ripple(cx: float, cy: float, t0: float, t1: float, r0: float, r1: float, color: str,
           width: float = 2.0, peak: float = 0.85) -> str:
    return (f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(r0)}" fill="none" stroke="{color}" '
            f'stroke-width="{f(width)}" opacity="0">'
            + anim("r", [(0, r0), (t0, r0), (t1, r1), (1, r1)], spline=True)
            + anim("opacity", [(0, 0), (t0, 0), (t0 + 0.004, peak), (t1, 0), (1, 0)])
            + "</circle>")


def stars(rng: random.Random) -> str:
    out = []
    for _ in range(90):
        x, y = rng.uniform(10, W - 10), rng.uniform(10, H - 10)
        r = rng.choice((0.6, 0.8, 1.0, 1.3))
        d = rng.uniform(0, 6)
        dur = rng.uniform(3.5, 7.5)
        op = rng.uniform(0.25, 0.7)
        out.append(f'<circle class="tw" cx="{f(x)}" cy="{f(y)}" r="{r}" fill="#cfd5ff" '
                   f'style="--o:{f(op)};animation-duration:{f(dur)}s;animation-delay:-{f(d)}s"/>')
    return "\n".join(out)


def build() -> str:
    rng = random.Random(7)
    ox, oy = O
    hx, hy = HUB

    # time marks of the story, fractions of the loop
    t_task = (0.045, 0.165)        # task: orchestrator → hub
    t_disp = (0.19, 0.29)          # dispatch: hub → workers
    t_work = (0.29, 0.50)          # workers working
    t_back = (0.50, 0.59)          # results: workers → hub
    t_gate = (0.595, 0.69)         # gates light one by one
    t_rev = (0.67, 0.80)           # review panel
    t_event = (0.80, 0.875)        # one line: hub → orchestrator
    t_decide = 0.875               # the orchestrator decides
    t_accept = (0.905, 0.955)      # accept: orchestrator → hub
    t_merge = (0.955, 0.995)       # merge ripple

    paths = {
        "pOH": f"M{f(ox)} {f(oy)} C330 105 520 105 {f(hx)} {f(hy)}",
        "pHO": f"M{f(hx)} {f(hy)} C520 295 330 295 {f(ox)} {f(oy)}",
    }
    for i, w in enumerate(WORKERS):
        paths[f"pW{i}"] = cubic(HUB, w)

    css = f"""
    .tw{{animation:tw 5s ease-in-out infinite;opacity:var(--o)}}
    @keyframes tw{{0%,100%{{opacity:var(--o)}}50%{{opacity:calc(var(--o)*.25)}}}}
    .au1{{animation:au1 22s ease-in-out infinite alternate}}
    .au2{{animation:au2 28s ease-in-out infinite alternate}}
    .au3{{animation:au3 25s ease-in-out infinite alternate}}
    @keyframes au1{{to{{transform:translate(120px,40px) scale(1.15)}}}}
    @keyframes au2{{to{{transform:translate(-140px,-30px) scale(.9)}}}}
    @keyframes au3{{to{{transform:translate(60px,-50px) scale(1.2)}}}}
    .spin{{animation:spin 48s linear infinite;transform-origin:{f(hx)}px {f(hy)}px}}
    .spinr{{animation:spin 70s linear infinite reverse;transform-origin:{f(hx)}px {f(hy)}px}}
    .ospin{{animation:spin 30s linear infinite;transform-origin:{f(ox)}px {f(oy)}px}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    .flow{{stroke-dasharray:2 7;animation:flow 3s linear infinite}}
    .flowr{{stroke-dasharray:2 7;animation:flow 3s linear infinite reverse}}
    @keyframes flow{{to{{stroke-dashoffset:-36}}}}
    .bob{{animation:bob 6s ease-in-out infinite}}
    @keyframes bob{{0%,100%{{opacity:.55}}50%{{opacity:1}}}}
    """

    defs = f"""
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#0a0d1c"/><stop offset=".55" stop-color="#111530"/>
      <stop offset="1" stop-color="#181b3c"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{CYAN}"/>
    </linearGradient>
    <linearGradient id="wire" gradientUnits="userSpaceOnUse" x1="{f(ox)}" y1="0" x2="1140" y2="0">
      <stop offset="0" stop-color="{WARM}"/><stop offset=".45" stop-color="{VIOLET}"/>
      <stop offset="1" stop-color="{CYAN}"/>
    </linearGradient>
    <linearGradient id="evt" gradientUnits="userSpaceOnUse" x1="{f(hx)}" y1="0" x2="{f(ox)}" y2="0">
      <stop offset="0" stop-color="{VIOLET}" stop-opacity=".2"/><stop offset="1" stop-color="{GOLD}"/>
    </linearGradient>
    <linearGradient id="sheen" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="300" y2="0">
      <stop offset="0" stop-color="#fff" stop-opacity="0"/>
      <stop offset=".5" stop-color="#fff" stop-opacity=".9"/>
      <stop offset="1" stop-color="#fff" stop-opacity="0"/>
      <animateTransform attributeName="gradientTransform" type="translate"
        values="-300 0;-300 0;1300 0" keyTimes="0;.55;1" dur="{T / 2}s" repeatCount="indefinite"/>
    </linearGradient>
    <radialGradient id="gWarm"><stop offset="0" stop-color="{WARM}" stop-opacity=".9"/>
      <stop offset=".35" stop-color="{WARM2}" stop-opacity=".35"/><stop offset="1" stop-color="{WARM2}" stop-opacity="0"/></radialGradient>
    <radialGradient id="gViolet"><stop offset="0" stop-color="#c4b5fd" stop-opacity=".9"/>
      <stop offset=".35" stop-color="{VIOLET}" stop-opacity=".35"/><stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>
    <radialGradient id="gCyan"><stop offset="0" stop-color="#a5f3fc" stop-opacity=".9"/>
      <stop offset=".35" stop-color="{CYAN}" stop-opacity=".3"/><stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></radialGradient>
    <radialGradient id="gMint"><stop offset="0" stop-color="#bbf7d0" stop-opacity=".9"/>
      <stop offset=".35" stop-color="{MINT}" stop-opacity=".3"/><stop offset="1" stop-color="{MINT}" stop-opacity="0"/></radialGradient>
    <radialGradient id="gGold"><stop offset="0" stop-color="#fff3c4" stop-opacity="1"/>
      <stop offset=".35" stop-color="{GOLD}" stop-opacity=".4"/><stop offset="1" stop-color="{GOLD}" stop-opacity="0"/></radialGradient>
    <radialGradient id="hubGlow"><stop offset="0" stop-color="{VIOLET}" stop-opacity=".42"/>
      <stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>
    <radialGradient id="orchGlow"><stop offset="0" stop-color="{WARM2}" stop-opacity=".38"/>
      <stop offset="1" stop-color="{WARM2}" stop-opacity="0"/></radialGradient>
    <radialGradient id="aV"><stop offset="0" stop-color="{VIOLET}" stop-opacity=".30"/>
      <stop offset="1" stop-color="{VIOLET}" stop-opacity="0"/></radialGradient>
    <radialGradient id="aC"><stop offset="0" stop-color="{CYAN}" stop-opacity=".16"/>
      <stop offset="1" stop-color="{CYAN}" stop-opacity="0"/></radialGradient>
    <radialGradient id="aW"><stop offset="0" stop-color="{WARM2}" stop-opacity=".14"/>
      <stop offset="1" stop-color="{WARM2}" stop-opacity="0"/></radialGradient>
    <radialGradient id="vign" cx=".5" cy=".45" r=".75"><stop offset=".6" stop-color="#000" stop-opacity="0"/>
      <stop offset="1" stop-color="#000" stop-opacity=".45"/></radialGradient>
    <pattern id="grid" width="32" height="32" patternUnits="userSpaceOnUse">
      <circle cx="16" cy="16" r=".8" fill="#8f97d6" opacity=".16"/></pattern>
    <filter id="soft" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="2.2"/></filter>
    <clipPath id="card"><rect width="{W}" height="{H}" rx="28"/></clipPath>
    """
    defs += "\n".join(f'<path id="{k}" d="{v}"/>' for k, v in paths.items())

    p: list[str] = []
    add = p.append

    # --- backdrop ---
    add(f'<rect width="{W}" height="{H}" fill="url(#bg)"/>')
    add(f'<ellipse class="au1" cx="330" cy="170" rx="420" ry="240" fill="url(#aW)"/>')
    add(f'<ellipse class="au2" cx="700" cy="230" rx="520" ry="300" fill="url(#aV)"/>')
    add(f'<ellipse class="au3" cx="1060" cy="190" rx="380" ry="260" fill="url(#aC)"/>')
    add(f'<rect width="{W}" height="{H}" fill="url(#grid)"/>')
    add(stars(rng))

    # --- wires: faint flowing guides ---
    add('<g fill="none" stroke-linecap="round">')
    add(f'<use href="#pOH" xlink:href="#pOH" class="flow" stroke="url(#wire)" stroke-width="1.4" opacity=".45"/>')
    add(f'<use href="#pHO" xlink:href="#pHO" class="flow" stroke="url(#wire)" stroke-width="1.4" opacity=".3"/>')
    for i in range(len(WORKERS)):
        op = ".42" if i in ACTIVE else ".2"
        add(f'<use href="#pW{i}" xlink:href="#pW{i}" class="flow" stroke="url(#wire)" stroke-width="1.2" opacity="{op}"/>')
    # the constellation between the workers
    chain = [0, 1, 2, 3, 4, 2, 0]
    pts = " ".join(f"{WORKERS[i][0]},{WORKERS[i][1]}" for i in chain)
    add(f'<polyline points="{pts}" stroke="{CYAN}" stroke-width=".8" opacity=".14"/>')
    add("</g>")

    # wires light up while a task travels on them
    def lit(path_id: str, t0: float, t1: float, color: str, width: float = 2.2) -> str:
        return (f'<use href="#{path_id}" xlink:href="#{path_id}" fill="none" stroke="{color}" '
                f'stroke-width="{width}" stroke-linecap="round" pathLength="1" stroke-dasharray="1 1" '
                f'stroke-dashoffset="1" opacity="0">'
                + anim("stroke-dashoffset", [(0, 1), (t0, 1), (t1, 0), (1, 0)], spline=True)
                + anim("opacity", [(0, 0), (t0, 0), (t0 + 0.01, 0.75), (min(t1 + 0.02, .985), 0.55),
                                   (min(t1 + 0.07, .997), 0), (1, 0)])
                + "</use>")

    add(lit("pOH", *t_task, WARM))
    for k, i in enumerate(ACTIVE):
        d = k * 0.02
        add(lit(f"pW{i}", t_disp[0] + d, t_disp[1] + d, VIOLET))
    add(lit("pHO", *t_event, "url(#evt)", 2.6))
    add(lit("pOH", *t_accept, MINT, 2))

    # --- orchestrator ---
    add(f'<circle cx="{f(ox)}" cy="{f(oy)}" r="120" fill="url(#orchGlow)">'
        + anim("opacity", [(0, .8), (0.07, 1), (0.16, .45), (t_event[1] - .01, .45), (t_decide + .01, 1.15),
                           (0.96, .8), (1, .8)]) + "</circle>")
    add(f'<g class="ospin"><circle cx="{f(ox)}" cy="{f(oy)}" r="62" fill="none" stroke="{WARM}" '
        f'stroke-opacity=".28" stroke-width="1" stroke-dasharray="3 9"/>'
        + "".join(f'<circle cx="{f(ox + 62 * math.cos(math.radians(a)))}" cy="{f(oy + 62 * math.sin(math.radians(a)))}" '
                  f'r="{r}" fill="{WARM}" opacity=".85"/>' for a, r in ((20, 3), (150, 2.2), (260, 2.6)))
        + "</g>")
    add(f'<circle cx="{f(ox)}" cy="{f(oy)}" r="38" fill="none" stroke="{WARM2}" stroke-width="2.4" opacity=".9"/>')
    add(f'<circle cx="{f(ox)}" cy="{f(oy)}" r="30" fill="#140f1c" stroke="{WARM}" stroke-opacity=".35"/>')
    # the core: bright while it files a task and decides, dim while it waits
    add(f'<circle cx="{f(ox)}" cy="{f(oy)}" r="14" fill="{WARM}">'
        + anim("opacity", [(0, .9), (0.06, 1), (0.17, .35), (t_event[1] - .005, .35), (t_decide + .006, 1),
                           (0.97, .9), (1, .9)])
        + anim("r", [(0, 14), (0.06, 16), (0.17, 11), (t_event[1] - .005, 11), (t_decide + .006, 18),
                     (0.95, 14), (1, 14)]) + "</circle>")
    add(f'<circle cx="{f(ox)}" cy="{f(oy)}" r="6" fill="#fff5e6" opacity=".9"/>')
    add(ripple(ox, oy, 0.02, 0.11, 38, 92, WARM, 1.6, 0.6))
    add(ripple(ox, oy, t_decide, t_decide + 0.075, 38, 135, GOLD, 2.4, 0.95))
    add(ripple(ox, oy, t_decide + 0.012, t_decide + 0.1, 38, 175, GOLD, 1.2, 0.5))

    # --- hub: the brand mark ---
    add(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="150" fill="url(#hubGlow)">'
        + anim("opacity", [(0, .75), (t_task[1], .75), (t_task[1] + .02, 1.2), (t_disp[1], .85),
                           (t_back[1], .85), (t_back[1] + .02, 1.15), (0.96, .8), (0.98, 1.25), (1, .75)]) + "</circle>")
    add(f'<g class="spinr"><circle cx="{f(hx)}" cy="{f(hy)}" r="62" fill="none" stroke="url(#accent)" '
        f'stroke-width="1" stroke-dasharray="1 6" opacity=".55"/></g>')
    spokes = [(-55, -42, 10, CYAN), (59, -35, 9, "#a78bfa"), (-42, 55, 8.4, "#a78bfa"), (53, 48, 10.8, CYAN)]
    add('<g class="spin">')
    add(f'<g stroke="url(#accent)" stroke-width="4.2" stroke-linecap="round" opacity=".9">'
        + "".join(f'<line x1="{f(hx)}" y1="{f(hy)}" x2="{f(hx + dx)}" y2="{f(hy + dy)}"/>' for dx, dy, _, _ in spokes)
        + "</g>")
    add("".join(f'<circle cx="{f(hx + dx)}" cy="{f(hy + dy)}" r="{f(r)}" fill="{c}"/>' for dx, dy, r, c in spokes))
    add("</g>")
    add(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="28" fill="url(#accent)"/>')
    add(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="12" fill="#0d1020"/>')
    # the core takes a task in and lets it out
    add(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="12" fill="#ede9fe" opacity="0">'
        + anim("opacity", [(0, 0), (t_task[1] - .005, 0), (t_task[1] + .005, .95), (t_disp[0] + .01, .2),
                           (t_back[1] - .005, 0), (t_back[1] + .005, .9), (t_gate[1], .15), (t_event[0], .6),
                           (t_event[0] + .02, 0), (t_accept[1] - .003, 0), (t_accept[1] + .006, 1), (0.99, 0), (1, 0)])
        + "</circle>")
    add(ripple(hx, hy, t_task[1], t_task[1] + 0.06, 28, 70, "#c4b5fd", 1.6, 0.7))
    add(ripple(hx, hy, t_merge[0], t_merge[1], 28, 160, MINT, 2.2, 0.9))
    add(ripple(hx, hy, t_merge[0] + 0.008, t_merge[1], 28, 115, "#bbf7d0", 1.2, 0.6))

    # gates: three rings that close one after another
    for k, r in enumerate(GATES_R):
        a = t_gate[0] + k * 0.03
        b = a + 0.035
        add(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="{r}" fill="none" stroke="{MINT}" stroke-width="{2.2 - k * .4}" '
            f'stroke-linecap="round" pathLength="1" stroke-dasharray="1 1" stroke-dashoffset="1" opacity="0" '
            f'transform="rotate(-90 {f(hx)} {f(hy)})">'
            + anim("stroke-dashoffset", [(0, 1), (a, 1), (b, 0), (1, 0)], spline=True)
            + anim("opacity", [(0, 0), (a, 0), (a + .006, .95), (b + .03, .7), (t_rev[1], .35), (t_rev[1] + .03, 0), (1, 0)])
            + "</circle>")
        add(f'<circle cx="{f(hx)}" cy="{f(hy)}" r="{r}" fill="none" stroke="{MINT}" stroke-width="6" '
            f'filter="url(#soft)" opacity="0">'
            + anim("opacity", [(0, 0), (b - .004, 0), (b, .55), (b + .04, 0), (1, 0)]) + "</circle>")

    # review panel: fresh sessions appear, look, agree
    for k, (rx, ry) in enumerate(REVIEWERS):
        a = t_rev[0] + k * 0.012
        ok = t_rev[0] + 0.05 + k * 0.022
        end = t_rev[1] + 0.02
        ux, uy = (rx - hx) / REVIEW_R, (ry - hy) / REVIEW_R
        x1, y1 = hx + ux * 34, hy + uy * 34
        x2, y2 = rx - ux * 10, ry - uy * 10
        add(f'<line x1="{f(x1)}" y1="{f(y1)}" x2="{f(x2)}" y2="{f(y2)}" stroke="#c4b5fd" stroke-width="1.3" '
            f'pathLength="1" stroke-dasharray="1 1" stroke-dashoffset="1" opacity="0">'
            + anim("stroke-dashoffset", [(0, 1), (a, 1), (a + .025, 0), (1, 0)], spline=True)
            + anim("opacity", window(a, end, .01, .6)) + "</line>")
        add(f'<g opacity="0">' + anim("opacity", window(a, end, .015))
            + f'<circle cx="{f(rx)}" cy="{f(ry)}" r="24" fill="url(#gViolet)"/>'
            + f'<circle cx="{f(rx)}" cy="{f(ry)}" r="9" fill="#120f24" stroke="#c4b5fd" stroke-width="1.6"/>'
            + f'<circle cx="{f(rx)}" cy="{f(ry)}" r="9" fill="{MINT}" opacity="0">'
            + anim("opacity", [(0, 0), (ok, 0), (ok + .006, 1), (1, 1)]) + "</circle>"
            + f'<circle cx="{f(rx)}" cy="{f(ry)}" r="28" fill="url(#gMint)" opacity="0">'
            + anim("opacity", [(0, 0), (ok, 0), (ok + .006, 1), (ok + .05, .35), (1, .35)]) + "</circle>"
            + f'<path d="M{f(rx - 3.6)} {f(ry + .2)} l2.6 2.6 l5 -5.4" fill="none" stroke="#062a1c" '
              f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0">'
            + anim("opacity", [(0, 0), (ok + .004, 0), (ok + .01, 1), (1, 1)]) + "</path>"
            + "</g>")

    # --- workers ---
    for i, (wx, wy) in enumerate(WORKERS):
        active = i in ACTIVE
        k = ACTIVE.index(i) if active else 0
        d = k * 0.02
        add(f'<circle class="bob" cx="{wx}" cy="{wy}" r="30" fill="url(#gCyan)" style="animation-delay:-{i * 1.3}s"/>')
        add(f'<circle cx="{wx}" cy="{wy}" r="11" fill="#081a22" stroke="{CYAN}" stroke-width="1.8" opacity=".95"/>')
        add(f'<circle cx="{wx}" cy="{wy}" r="4.2" fill="{CYAN}" opacity=".9"/>')
        if not active:
            continue
        w0, w1 = t_work[0] + d, t_work[1] + d * 0.5
        # the node heats up while it works
        add(f'<circle cx="{wx}" cy="{wy}" r="46" fill="url(#gCyan)" opacity="0">'
            + anim("opacity", [(0, 0), (w0 - .01, 0), (w0 + .01, 1), (w0 + .06, .55), (w0 + .11, 1), (w0 + .16, .6),
                               (w1, 1), (w1 + .04, 0), (1, 0)]) + "</circle>")
        add(f'<circle cx="{wx}" cy="{wy}" r="4.2" fill="#ecfeff" opacity="0">'
            + anim("opacity", window(w0, w1 + .02, .01)) + "</circle>")
        # a progress ring that fills
        add(f'<circle cx="{wx}" cy="{wy}" r="20" fill="none" stroke="{CYAN}" stroke-width="2" stroke-linecap="round" '
            f'pathLength="1" stroke-dasharray="1 1" stroke-dashoffset="1" opacity="0" transform="rotate(-90 {wx} {wy})">'
            + anim("stroke-dashoffset", [(0, 1), (w0, 1), (w1, 0), (1, 0)], spline=True)
            + anim("opacity", window(w0, w1 + .03, .01, .95)) + "</circle>")
        # sparks orbiting the busy node
        add(f'<g opacity="0">' + anim("opacity", window(w0, w1, .015))
            + f'<g><animateTransform attributeName="transform" type="rotate" from="0 {wx} {wy}" to="360 {wx} {wy}" '
              f'dur="{2.2 + k * .4}s" repeatCount="indefinite"/>'
            + f'<circle cx="{wx + 28}" cy="{wy}" r="2" fill="#a5f3fc"/>'
            + f'<circle cx="{wx - 28}" cy="{wy}" r="1.4" fill="#a5f3fc" opacity=".7"/></g>'
            + f'<g><animateTransform attributeName="transform" type="rotate" from="360 {wx} {wy}" to="0 {wx} {wy}" '
              f'dur="{3.4 + k * .3}s" repeatCount="indefinite"/>'
            + f'<circle cx="{wx}" cy="{wy - 35}" r="1.6" fill="{VIOLET}"/></g></g>')
        add(ripple(wx, wy, t_disp[1] + d, t_disp[1] + d + 0.06, 11, 40, CYAN, 1.4, 0.8))

    # --- travelling signals ---
    add(comet("pOH", *t_task, "#fff1dc", "gWarm"))
    for k, i in enumerate(ACTIVE):
        d = k * 0.02
        add(comet(f"pW{i}", t_disp[0] + d, t_disp[1] + d, "#ede9fe", "gViolet", size=.85))
        add(comet(f"pW{i}", t_back[0] + d * .5, t_back[1] - .01 + d * .5, "#ecfeff", "gCyan", reverse=True, size=.85))
    add(comet("pHO", *t_event, "#fffbea", "gGold", size=1.1))
    add(comet("pOH", *t_accept, "#ecfdf5", "gMint", size=.9))

    # --- labels ---
    lab = f'font-family="{MONO}" font-size="11.5" letter-spacing="3" fill="#8e94bf" text-anchor="middle"'
    add(f'<text x="{f(ox)}" y="348" {lab}>ORCHESTRATOR</text>')
    add(f'<text x="{f(hx)}" y="348" {lab}>THE HUB</text>')
    add(f'<text x="1075" y="372" {lab}>WORKER MODELS</text>')

    # one subtitle at a time, like a caption track
    captions = [
        (0.015, 0.17, "files a task — and goes quiet"),
        (0.18, 0.30, "the hub hands it to cheap models"),
        (0.30, 0.50, "each works in its own worktree"),
        (0.51, 0.665, "results come back through the gates"),
        (0.665, 0.80, "a review panel in fresh sessions"),
        (0.80, 0.885, "one line wakes the orchestrator"),
        (0.885, 0.995, "it decides. the hub merges."),
    ]
    for a, b, text in captions:
        add(f'<text x="{W / 2}" y="398" font-family="{MONO}" font-size="14" fill="#c9cde6" text-anchor="middle" '
            f'opacity="0">{anim("opacity", window(a, b, .015))}{text}</text>')

    # --- wordmark ---
    add(f'<line x1="540" y1="424" x2="740" y2="424" stroke="url(#accent)" stroke-width="1" opacity=".35"/>')
    word = (f'x="{W / 2}" y="482" font-family="{SANS}" font-size="56" font-weight="700" '
            f'letter-spacing="-1" text-anchor="middle"')
    add(f'<text {word} fill="#ffffff">agent-hub</text>')
    add(f'<text {word} fill="url(#sheen)" opacity=".55">agent-hub</text>')
    add(f'<text x="{W / 2}" y="514" font-family="{SANS}" font-size="18" fill="#a9afd6" text-anchor="middle">'
        f'Claude decides. Cheap models do the work.</text>')

    add(f'<rect width="{W}" height="{H}" fill="url(#vign)"/>')
    add(f'<rect x=".5" y=".5" width="{W - 1}" height="{H - 1}" rx="27.5" fill="none" stroke="#ffffff" stroke-opacity=".06"/>')

    body = "\n".join(p)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{W}" height="{H}" viewBox="0 0 {W} {H}" role="img" aria-label="agent-hub: the orchestrator '
            f'files a task, the hub runs it on cheap worker models through gates and review, one line comes back, '
            f'the orchestrator decides">\n'
            f"<title>agent-hub</title>\n<style>{css}</style>\n<defs>{defs}</defs>\n"
            f'<g clip-path="url(#card)">\n{body}\n</g>\n</svg>\n')


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "docs" / "assets" / "hero.svg"
    out.write_text(build(), encoding="utf-8")
    print(f"{out} ({out.stat().st_size // 1024} KB)")
