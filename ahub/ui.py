"""The one rendering layer for human output: styled(), badge(), rule(), section(), kv(), para(), bullets(),
table(), fit(), Live().

Three rules keep it small:
- colour is a hint — ANSI only when stdout is a TTY and NO_COLOR is unset (ahub is read through a pipe by
  Claude, so that output must stay plain and compact);
- every block takes the width explicitly or takes it from COLUMNS/the terminal (fallback 100), so the
  layout is deterministic in tests;
- a long operation shows one live line only on a TTY (`Live`); a pipe gets nothing at all.

Words are never hardcoded here — the caller passes them through t().
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import textwrap
from collections.abc import Iterable, Sequence
from types import TracebackType
from typing import Any

DEFAULT_WIDTH = 100
MIN_WIDTH = 40
MIN_COL = 4  # a table column narrower than this is useless
GAP = 2
BULLET = "•"
RULE = "─"
ELLIPSIS = "…"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
CLEAR_LINE = "\r\033[K"

_CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
          "magenta": "35", "cyan": "36", "grey": "90"}
PULSE_STYLE = {"working": "green", "waiting": "yellow", "silent": "red", "dead": "magenta", "unknown": "dim"}
_ANSI = re.compile(r"\033\[[0-9;]*m")
_ITEM = re.compile(r"^([-*•]|\d+\.)\s+(.*)$")
_SENTENCE = re.compile(r"[.!?…](?=\s|$)")
_PARAGRAPH = re.compile(r"\n[ \t]*\n")

Value = str | Sequence[Any]  # a kv value: text, or the chunks of the line (a string, or a (label, value) pair)


def width(explicit: int | None = None) -> int:
    """The width to draw for: the explicit one, else COLUMNS/the terminal, else 100."""
    if explicit is not None:
        return max(MIN_WIDTH, int(explicit))
    return max(MIN_WIDTH, shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns or DEFAULT_WIDTH)


def colour_on() -> bool:
    """True — a human at a terminal that wants colour. A pipe or NO_COLOR — plain text."""
    try:
        tty = sys.stdout.isatty()
    except (AttributeError, ValueError):  # a closed or exotic stream — treat it as a pipe
        tty = False
    return bool(tty) and "NO_COLOR" not in os.environ


def styled(text: str, *styles: str) -> str:
    """Colour/bold the text when stdout can show it; otherwise — the text as it is."""
    if not styles or not colour_on():
        return text
    codes = ";".join(_CODES[s] for s in styles if s in _CODES)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def badge(mark: str, word: str, pulse: str = "") -> str:
    """The pulse symbol + the state word; the colour only repeats the pulse, it is never the message."""
    return styled(f"{mark} {word}".strip(), PULSE_STYLE.get(pulse, ""))


def plain_len(text: str) -> int:
    """The width as it is seen — the ANSI colour codes take no columns (for indenting what follows)."""
    return len(_ANSI.sub("", text))


def rule(n: int) -> str:
    return styled(RULE * max(3, n), "dim")


def section(title: str) -> str:
    """A block heading (bold on a terminal, a plain word in a pipe)."""
    return styled(title, "bold")


def clip(text: Any, n: int) -> str:
    """Shorten to n characters at a word boundary with an ellipsis — for narrow columns only."""
    s = " ".join(str(text or "").split())
    if len(s) <= n:
        return s
    if n <= 1:
        return ELLIPSIS[:n]
    head = s[: n - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,;:-·—") + ELLIPSIS


def _item_lines(item: str, body: int) -> list[str]:
    """One list item, wrapped with a hanging indent under the marker."""
    m = _ITEM.match(item)
    if not m:
        return [item]
    mark = f"{m.group(1)} "
    lines = textwrap.wrap(m.group(2), max(12, body - len(mark)), break_long_words=False,
                          break_on_hyphens=False) or [""]
    return [mark + lines[0]] + [" " * len(mark) + ln for ln in lines[1:]]


def para(text: Any, *, indent: int = 2, w: int | None = None) -> str:
    """Wrap to the width at word boundaries. Paragraphs and list items are kept, no word is ever cut."""
    body = max(20, width(w) - indent)
    pad = " " * indent
    blocks: list[str] = []
    for block in _PARAGRAPH.split(str(text or "").replace("\r\n", "\n").strip()):
        items = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if not items:
            continue
        lines = [pad + ln for item in items for ln in _item_lines(item, body)] if all(_ITEM.match(i) for i in items) \
            else [pad + ln for ln in textwrap.wrap(" ".join(items), body, break_long_words=False,
                                                    break_on_hyphens=False) or [""]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def bullets(items: Iterable[Any], *, indent: int = 2, w: int | None = None) -> str:
    """One item per line, wrapped under the bullet."""
    body = max(20, width(w) - indent - 2)
    out: list[str] = []
    for item in items:
        lines = textwrap.wrap(" ".join(str(item or "").split()), body, break_long_words=False,
                              break_on_hyphens=False) or [""]
        out.append(" " * indent + BULLET + " " + lines[0])
        out.extend(" " * (indent + 2) + ln for ln in lines[1:])
    return "\n".join(out)


def _chunk(item: Any) -> tuple[str, str]:
    return (str(item[0]), str(item[1])) if isinstance(item, tuple) else ("", str(item))


def _chunks(rows: Sequence[tuple[str, Value]]) -> list[list[tuple[str, str]]]:
    """Normalize rows to a list of (label, value) cells; empty rows are dropped. In a chunk list the row
    label names the first chunk; the rest get their own labels (a chunk with no value is skipped)."""
    out: list[list[tuple[str, str]]] = []
    for label, value in rows:
        if isinstance(value, str):
            cells = [(str(label), value)]
        else:
            items = [_chunk(v) for v in value]
            cells = [(str(label), items[0][1]), *[i for i in items[1:] if i[1]]] if items else []
        cells = [(a, " ".join(b.split())) for a, b in cells]
        if cells and any(b for _, b in cells):
            out.append(cells)
    return out


def kv(rows: Sequence[tuple[str, Value]], *, indent: int = 0, gap: int = GAP, w: int | None = None) -> str:
    """An aligned "label  value" block. The value may be the chunks of the line — the first is the value of
    the label, the rest are their own label/value pairs, so a block reads as a table without a header.
    The last cell wraps under its label."""
    cells = _chunks(rows)
    if not cells:
        return ""
    ncols = max(len(c) for c in cells)
    lw = [max(len(c[i][0]) for c in cells if len(c) > i) for i in range(ncols)]
    vw = [max(len(c[i][1]) for c in cells if len(c) > i) for i in range(ncols)]
    pad = " " * indent
    out: list[str] = []
    for c in cells:
        head = pad
        for i, (label, value) in enumerate(c[:-1]):
            if label:
                head += label.ljust(lw[i]) + " " * gap
            head += value.ljust(vw[i]) + " " * gap
        label, value = c[-1]
        if label:
            head += label.ljust(lw[len(c) - 1]) + " " * gap
        if not value:
            out.append(head.rstrip())
            continue
        lines = para(value, indent=len(head), w=w).split("\n")
        out.append(head + lines[0][len(head):])
        out.extend(lines[1:])
    return "\n".join(ln.rstrip() for ln in out)


def table(head: Sequence[str] | None, rows: Sequence[Sequence[Any]], *, max_width: Sequence[int | None] | None = None,
          indent: int = 0, gap: int = GAP, w: int | None = None) -> str:
    """Columns from the content, shrunk to fit the width — an ellipsis appears in a cell only.
    head=None — no column head (the rows speak for themselves)."""
    head = list(head) if head else []
    cols = max([len(head)] + [len(r) for r in rows]) if rows else len(head)
    body = [list(r) + [""] * (cols - len(r)) for r in rows]
    widths = [max(([len(str(head[i]))] if head else [0]) + [len(str(r[i])) for r in body])
              for i in range(cols)]
    caps = list(max_width or []) + [None] * (cols - len(max_width or []))
    total = max(20, width(w) - indent)
    for i in range(cols):
        widths[i] = min(widths[i], max(MIN_COL, caps[i] or total))
    over = sum(widths) + gap * (cols - 1) - total
    while over > 0:  # take from the widest column that still has something to give
        cand = [i for i in range(cols) if widths[i] > MIN_COL]
        if not cand:
            break
        i = max(cand, key=lambda k: widths[k])
        cut = min(over, widths[i] - MIN_COL)
        widths[i] -= cut
        over -= cut
    pad = " " * indent
    out = []
    if head:
        out.append((pad + (" " * gap).join(styled(clip(head[i], widths[i]).ljust(widths[i]), "dim")
                                          for i in range(cols))).rstrip())
    for r in body:
        out.append((pad + (" " * gap).join(clip(r[i], widths[i]).ljust(widths[i])
                                          for i in range(cols))).rstrip())
    return "\n".join(out)


def _sentence_cut(text: str, budget: int) -> str:
    """The longest prefix within the byte budget that ends at a sentence or a word boundary."""
    if budget <= 0:
        return ""
    head = text.encode("utf-8")[:budget].decode("utf-8", "ignore").rstrip()
    if not head:
        return ""
    ends = [m.end() for m in _SENTENCE.finditer(head)]
    if ends and ends[-1] >= len(head) // 2:
        return head[: ends[-1]].rstrip()
    return head.rsplit(" ", 1)[0].rstrip(" ,;:-") if " " in head else head


def fit(text: Any, limit: int, hint: str = "") -> str:
    """Cut the text to `limit` bytes at a paragraph or sentence end (never inside a word). The hint —
    where the whole text is — goes on its own line under what is left."""
    s = str(text or "").replace("\r\n", "\n").strip()
    if not s:
        return ""
    if len(s.encode("utf-8")) <= limit:
        return s
    budget = max(64, limit - (len(hint.encode("utf-8")) + 2 if hint else 0))
    kept: list[str] = []
    used = 0
    for block in _PARAGRAPH.split(s):
        block = block.strip()
        if not block:
            continue
        size = len(block.encode("utf-8")) + 2
        if used + size > budget:
            tail = _sentence_cut(block, budget - used)
            if tail:
                kept.append(tail + ELLIPSIS)
            break
        kept.append(block)
        used += size
    body = "\n\n".join(kept)
    return f"{body}\n\n{hint}" if hint else body  # the hint is its own paragraph


class Live:
    """One live line for a long operation: a spinner, or progress `label… n/total`.

    Only on a TTY — into a pipe it writes nothing (a `\r` line would be noise for Claude). The line is
    cleared when the block ends, whatever happens; `step()` refreshes it (and moves the spinner on).

        with Live(t("models.checking"), total=len(aliases)) as p:
            for alias in aliases:
                ...
                p.step()
    """

    def __init__(self, label: str, total: int = 0, out: Any = None) -> None:
        self._label = label
        self._total = int(total)
        self._done = 0
        self._frame = -1
        self._live = colour_on()
        self._out = out if out is not None else sys.stdout

    def __enter__(self) -> "Live":
        self.draw()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.clear()

    def draw(self) -> None:
        if not self._live:
            return
        text = self._label
        if self._total:
            text = f"{text} {self._done}/{self._total}"
        elif self._frame >= 0:
            text = f"{SPINNER[self._frame % len(SPINNER)]} {text}"
        try:
            self._out.write(CLEAR_LINE + text)
            self._out.flush()
        except (OSError, ValueError):  # a closed or exotic stream — drop the line, keep the work
            self._live = False

    def clear(self) -> None:
        if not self._live:
            return
        self._live = False
        try:
            self._out.write(CLEAR_LINE)
            self._out.flush()
        except (OSError, ValueError):
            pass

    def step(self, n: int = 1) -> None:
        """One item done (or `n` of them); the line is redrawn."""
        self._done += n
        self._frame += 1
        self.draw()

    def total(self, total: int) -> None:
        """The total, when it is known only after the work started."""
        self._total = int(total)
        self.draw()
