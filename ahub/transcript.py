"""Readable transcript of a worker session: `ahub follow T12` (`ahub log T12` stays raw).

The raw provider stream stays in `.ahub/logs/<name>.log` (JSON lines, the turns of a session append to
the same file), and the engine writes the prompt of every turn into the sidecar `<log>.prompts.jsonl`.
Here the two become a readable transcript: items come from the provider's own parse_line (the provider is
taken from the session row), the sidecar gives the turn headers, and the rendering is plain text with
ANSI colors only for a TTY.

Turns are matched to prompts by order. A provider whose events carry their own timestamps (opencode) is
matched by the prompt time; one without them (agy, codex) by the session/thread id that every process run
starts with — `Provider.stamped` says which it is.
"""

from __future__ import annotations

import json
import re
import shutil
import sys
from codecs import getincrementaldecoder
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from ahub.i18n import t as _t
from ahub.providers.base import Act, Activity, Provider, Usage
from ahub.time import now_ms, to_local
from ahub.ui import clip

PROMPT_SUFFIX = ".prompts.jsonl"
PROMPT_KINDS = ("start", "continue", "repair", "rework", "stop", "review", "nudge")
PROMPT_LINES = 20  # lines of the prompt shown without --full
RESULT_LINES = 6  # lines of a tool result
RESULT_COLS = 120  # width of a result line
ARGS_COLS = 160  # width of the arguments of a tool call
REASON_CHARS = 160

_RUN_TOOLS = frozenset({"bash", "run_command", "command_execution", "notebook_execution", "shell",
                        "send_command_input"})
_EDIT_TOOLS = frozenset({"edit", "write", "patch", "multiedit", "apply_patch", "patch_apply", "file_change",
                         "write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file",
                         "notebook_edit"})
_SEARCH_TOOLS = frozenset({"grep", "grep_search", "glob", "search", "websearch", "web_search", "webfetch",
                           "fetch", "search_web", "read_url_content", "open_browser_url"})
_READ_TOOLS = frozenset({"read", "list", "glob", "view_file", "list_dir", "find_by_name", "read_resource",
                         "read_browser_page", "list_resources"})
_CMD_KEYS = ("command", "commandline", "cmd", "script")
_PATH_KEYS = ("filepath", "file_path", "path", "targetfile", "paths", "file", "filename", "notebook_path")
_QUERY_KEYS = ("query", "pattern", "q", "regex", "url", "glob", "description", "prompt")
_OK_STATUS = ("", "completed", "done", "success", "ok")
_BAD_STATUS = ("error", "failed", "failure", "denied", "rejected")
_BASH_WRAP = re.compile(r"^/bin/(?:ba)?sh -lc ['\"](.*)['\"]$", re.DOTALL)  # codex wraps every command

# Model text and tool output are content the worker did not write: a `cat` of a hostile file, a fetched page.
# OSC 52 (clipboard), OSC 0 (window title) and the rest of the terminal language must not reach the owner's
# terminal from it — what a line says is printed, the sequences are dropped (ahub/transcript.py `safe`).
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")  # OSC … BEL|ST
_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # CSI … final byte (colours, cursor moves)
_ESC = re.compile(r"\x1b[@-Z\\-_]")  # the two-byte escapes
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # C0 (tab and newline stay) and DEL

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RED = "\x1b[31m"
_GREEN = "\x1b[32m"


def prompts_path(log_path: str | Path) -> Path:
    """Sidecar with the prompt of every turn of one session log: `<log>.prompts.jsonl`."""
    return Path(f"{log_path}{PROMPT_SUFFIX}")


@dataclass(frozen=True)
class Prompt:
    """One line of the sidecar: the prompt of one turn."""

    ts: int
    turn: int
    kind: str
    text: str


@dataclass(frozen=True)
class Item:
    """One readable piece of a turn: model text, a tool call, its result, an error, usage."""

    kind: str  # text | reasoning | tool | result | error | usage
    ts: int
    turn: int = -1  # 1-based number of the turn, -1 — before the first turn
    text: str = ""
    tool: str = ""
    args: str = ""
    status: str = ""
    output: str = ""
    usage: Usage | None = None
    cost: float | None = None  # money of the step, when the provider reports it next to the tokens


@dataclass
class Chunk:
    """What appeared since the previous read: new prompts and new items (in order)."""

    prompts: list[Prompt] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.prompts or self.items)


def _prompt_of(line: str) -> Prompt | None:
    """One sidecar line → Prompt (a broken line is skipped)."""
    try:
        raw = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(raw, dict) or "turn" not in raw:
        return None
    try:
        turn = int(raw["turn"])
    except (TypeError, ValueError):
        return None
    return Prompt(int(raw.get("ts") or 0), turn, str(raw.get("kind") or ""), str(raw.get("text") or ""))


def read_prompts(path: str | Path) -> list[Prompt]:
    """All prompts of a sidecar (no file — no prompts)."""
    out: list[Prompt] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                p = _prompt_of(line)
                if p is not None:
                    out.append(p)
    except OSError:
        return []
    return out


def tool_icon(tool: str) -> str:
    """One icon per kind of tool: run / edit / read / search."""
    name = (tool or "").lower()
    if name in _RUN_TOOLS:
        return "▶"
    if name in _EDIT_TOOLS:
        return "✎"
    if name in _SEARCH_TOOLS:
        return "🔎"
    return "👁"


def _flat(value) -> str:
    """One line from a tool argument: a string, or the first two of a list; nothing else."""
    if isinstance(value, str):
        return " ".join(value.split())[:200]
    if isinstance(value, list):
        return ", ".join(x for x in (_flat(v) for v in value[:2]) if x)
    return ""


def _pick(inp: dict, key: str) -> str:
    """Argument value by key, ignoring the case of the key (agy keeps CommandLine, TargetFile)."""
    for k, v in inp.items():
        if isinstance(k, str) and k.lower() == key:
            return _flat(v)
    return ""


def compact_args(tool: str, data: dict) -> str:
    """The interesting argument of a tool call: the command, the path or the query."""
    inp = data.get("input") if isinstance(data, dict) else None
    if not isinstance(inp, dict):
        return ""
    name = (tool or "").lower()
    if name in _RUN_TOOLS:
        keys = _CMD_KEYS + _PATH_KEYS
    elif name in _EDIT_TOOLS or name in _READ_TOOLS:
        keys = _PATH_KEYS + _QUERY_KEYS + _CMD_KEYS
    elif name in _SEARCH_TOOLS:
        keys = _QUERY_KEYS + _PATH_KEYS + _CMD_KEYS
    else:  # a tool we do not know: the first argument that says anything
        keys = _CMD_KEYS + _PATH_KEYS + _QUERY_KEYS
    for key in keys:
        got = _pick(inp, key)
        if got:
            return _args_line(key in _CMD_KEYS, got)
    for raw in inp.values():
        got = _flat(raw)
        if got:
            return _args_line(False, got)
    return ""


def _args_line(is_command: bool, text: str) -> str:
    wrapped = _BASH_WRAP.match(text)  # codex: `/bin/bash -lc '…'` — the shell is noise
    if is_command and wrapped:
        text = wrapped.group(1)
    return clip(text, ARGS_COLS)


def result_output(data: dict) -> str:
    """Tool result output, if the provider puts it into the activity (opencode, codex; agy has none)."""
    out = data.get("output") if isinstance(data, dict) else ""
    return out if isinstance(out, str) else ""


def _one_line(text: str, limit: int) -> str:
    return " ".join((text or "").split())[:limit]


def _clipped(text: str, cols: int) -> list[str]:
    lines = (text or "").replace("\r", "").splitlines()
    return [(ln[: cols - 1] + "…") if len(ln) > cols else ln for ln in lines]


def _num(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float)) and abs(value) >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    return str(value)


def _money(value: float) -> str:
    return f"{value:.4f}" if value >= 0.0001 else f"{value:.6f}"


def _usage_line(it: "Item") -> str:
    """Tokens of a step and, when the provider reports money, the cost; agy/codex report no prices."""
    u = it.usage or Usage()
    cost = it.cost if it.cost is not None else (u.cost_go or 0.0) + (u.cost_usd or 0.0)
    return _t("follow.usage", inn=_num(u.tokens_in), out=_num(u.tokens_out), cache=_num(u.cache_read or 0),
               cost=_t("follow.money", cost=_money(cost)) if cost else "")


def safe(text: str) -> str:
    """Text of a model or a tool without the control sequences a terminal would act on."""
    return _CONTROL.sub("", _ESC.sub("", _CSI.sub("", _OSC.sub("", text or ""))))


def paint(text: str, code: str, color: bool) -> str:
    """ANSI code around a fragment — only when the transcript goes to a terminal."""
    return f"{code}{text}{_RESET}" if color and text else text


def dim(text: str, color: bool = False) -> str:
    return paint(text, _DIM, color)


class _Tail:
    """A file that only grows: complete lines since the previous read, a partial tail waits.

    Decoding is incremental: a multi-byte character split across two reads is one character, not a
    replacement sign and a lost half.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._off = 0
        self._buf = ""
        self._dec = getincrementaldecoder("utf-8")("replace")

    def lines(self) -> list[str]:
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        if size < self._off:  # the file was replaced — read it from the start
            self._off, self._buf = 0, ""
            self._dec.reset()
        if size == self._off:
            return []
        try:
            with self.path.open("rb") as f:
                f.seek(self._off)
                data = f.read()
        except OSError:
            return []
        self._off += len(data)
        parts = (self._buf + self._dec.decode(data)).split("\n")
        self._buf = parts.pop()
        return [ln.rstrip("\r") for ln in parts if ln.strip()]


class Reader:
    """Reads a session log and its prompts sidecar incrementally (both files only grow).

    `read()` returns what appeared since the previous call; the first call returns the whole history,
    so the same code serves a finished task and a live tail.
    """

    def __init__(self, provider: Provider, log_path: str | Path) -> None:
        self.provider = provider
        self.path = Path(log_path)
        self.prompts_file = prompts_path(self.path)
        self.prompts: list[Prompt] = []
        self._log = _Tail(self.path)
        self._side = _Tail(self.prompts_file)
        self._runs = 0  # process runs seen (a provider without its own timestamps)

    def read(self, now: int | None = None) -> Chunk:
        """New prompts and new items since the previous read."""
        chunk = Chunk()
        last = self.prompts[-1].turn if self.prompts else 0
        for line in self._side.lines():
            p = _prompt_of(line)
            if p is not None and p.turn > last:
                self.prompts.append(p)
                chunk.prompts.append(p)
                last = p.turn
        stamp = now if now is not None else now_ms()
        acts: list[Activity] = []
        for line in self._log.lines():
            try:
                acts.extend(self.provider.parse_line(line, stamp))
            except Exception:  # a broken line of the raw log must not break the transcript
                continue
        chunk.items = self._items(acts)
        return chunk

    def _items(self, acts: list[Activity]) -> list[Item]:
        """Activities → items, each with the turn it belongs to."""
        stamped = bool(getattr(self.provider, "stamped", False))
        out: list[Item] = []
        started: list[tuple[str, str]] = []  # tool calls that got a start line
        for a in acts:
            if a.kind is Act.SESSION and not stamped and self._runs < len(self.prompts):
                self._runs += 1  # a run of an unstamped provider starts with its own session id
            turn = self._turn_of(a, stamped)
            data = a.data if isinstance(a.data, dict) else {}
            if a.kind is Act.TEXT:
                out.append(Item("text", a.ts, turn, text=a.text.strip()))
            elif a.kind is Act.REASONING:
                out.append(Item("reasoning", a.ts, turn, text=_one_line(a.text, REASON_CHARS)))
            elif a.kind is Act.TOOL_START:
                started.append((a.tool, compact_args(a.tool, data)))
                out.append(Item("tool", a.ts, turn, tool=a.tool, args=started[-1][1]))
            elif a.kind is Act.TOOL_END:
                key = (a.tool, compact_args(a.tool, data))
                if key in started:
                    started.remove(key)
                else:  # the provider reported only the finished call (opencode)
                    out.append(Item("tool", a.ts, turn, tool=a.tool, args=key[1]))
                out.append(Item("result", a.ts, turn, tool=a.tool, args=key[1],
                                status=str(data.get("status") or ""), output=result_output(data)))
            elif a.kind is Act.ERROR:
                out.append(Item("error", a.ts, turn, text=a.text))
            elif a.kind is Act.USAGE and isinstance(data.get("usage"), Usage):
                cost = data.get("cost")
                out.append(Item("usage", a.ts, turn, usage=data["usage"],
                                cost=cost if isinstance(cost, (int, float)) else None))
        return out

    def _turn_of(self, act: Activity, stamped: bool) -> int:
        """Number of the turn an activity belongs to (1-based, -1 — before the first turn)."""
        if not stamped:
            return self._runs if self._runs else -1
        turn = -1
        for i, p in enumerate(self.prompts, start=1):
            if p.ts > act.ts:
                break
            turn = i
        return turn


class Writer:
    """Renders the transcript of one session to a terminal: turn headers, prompts, text, tool calls,
    results, errors, usage. Colors are ANSI escapes only when the output is a TTY."""

    def __init__(self, out: TextIO | None = None, *, full: bool = False, color: bool | None = None,
                 width: int = 0) -> None:
        self.out: TextIO = out if out is not None else sys.stdout
        self.full = full
        self.color = self.out.isatty() if color is None else color
        self.width = width or shutil.get_terminal_size((100, 24)).columns
        self.prompts: dict[int, Prompt] = {}
        self._shown: set[int] = set()
        self._last = -1
        self._printed = False
        self._tail = (-1, "")  # (turn, text already shown) — a growing buffer is shown once

    def write(self, chunk: Chunk) -> None:
        for p in chunk.prompts:
            self.prompts.setdefault(p.turn, p)
        for it in chunk.items:
            if it.turn > self._last:
                for turn in range(max(self._last + 1, 1), it.turn + 1):
                    self._header(turn)
                self._last = it.turn
            self._item(it)
        if chunk:
            self.out.flush()

    def flush(self) -> None:
        """Headers of turns that produced nothing at all (the process never started)."""
        for turn in sorted(self.prompts):
            if turn not in self._shown:
                self._header(turn)
        self.out.flush()

    # --- internals ---

    def _paint(self, text: str, code: str) -> str:
        return paint(text, code, self.color)

    def _line(self, text: str) -> None:
        self._printed = True
        self.out.write(text + "\n")

    def _header(self, turn: int) -> None:
        self._shown.add(turn)
        p = self.prompts.get(turn)
        if p is None:
            return
        if self._printed:
            self._line("")
        at = to_local(p.ts).strftime("%H:%M") if p.ts else "—"
        self._line(self._paint(f"── Turn {p.turn} · {p.kind or '?'} · {at} ──", _BOLD))
        lines = safe(p.text).rstrip().splitlines()
        shown = lines if self.full else lines[:PROMPT_LINES]
        for ln in shown:
            self._line(self._paint(f"  {ln}", _DIM))
        if len(lines) > len(shown):
            self._line(self._paint("  " + _t("follow.more", n=len(lines) - len(shown)), _DIM))
        self._line("")

    def _item(self, it: Item) -> None:
        if it.kind == "text":
            self._text(it)
        elif it.kind == "reasoning":
            self._line(self._paint(_t("follow.reasoning", text=safe(it.text)), _DIM))
        elif it.kind == "tool":
            self._line(safe(f"{tool_icon(it.tool)} {it.tool}{'  ' + it.args if it.args else ''}".rstrip()))
        elif it.kind == "result":
            self._result(it)
        elif it.kind == "error":
            for i, ln in enumerate(safe(it.text).splitlines() or [""]):
                self._line(self._paint(f"{'✖ ' if i == 0 else '  '}{ln}", _RED))
        elif it.kind == "usage" and it.usage is not None:
            self._line(self._paint(_usage_line(it), _DIM))

    def _text(self, it: Item) -> None:
        """Model text as is; a provider that repeats a growing buffer (agy) shows only what is new."""
        turn, shown = self._tail
        growing = turn == it.turn and bool(shown) and it.text.startswith(shown)
        text = it.text[len(shown):].lstrip("\n") if growing else it.text
        self._tail = (it.turn, it.text)
        for ln in safe(text).splitlines():
            self._line(ln)

    def _result(self, it: Item) -> None:
        status = safe(it.status).strip().lower()
        mark = "✔" if status in _OK_STATUS else "✖" if status in _BAD_STATUS else "·"
        code = _GREEN if mark == "✔" else _RED if mark == "✖" else _DIM
        self._line(self._paint(f"  {mark} {status or '—'}", code))
        lines = _clipped(safe(it.output), max(20, min(RESULT_COLS, self.width - 4)))
        for ln in lines[:RESULT_LINES]:
            self._line(self._paint(f"    {ln}", _DIM))
        if len(lines) > RESULT_LINES:  # a result is cut at RESULT_LINES in any case — --full does not change it
            self._line(self._paint("    " + _t("follow.result_more", n=len(lines) - RESULT_LINES), _DIM))
