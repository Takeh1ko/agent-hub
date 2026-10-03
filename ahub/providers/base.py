"""Model provider contract (architecture §3).

A provider handles one model source (opencode, agy, later Codex…).
Shared process mechanics (stream, silence, timeout, stop, log) live in
ahub.providers.runner — same for all. Each provider module only owns its part:

- build_command / env — how to start or resume a session;
- parse_line — output line → normalized activity (Activity), incl. session id and errors;
- classify — finished process → Outcome (provider classifies, core decides);
- final_text / structured — last reply and structured result;
- usage / session_state / find_session / export — provider-side session data (usage, pulse, fallback id, log);
- catalog / health — models and health.

Each module declares `capabilities`. Whatever is missing returns None, and the core
reports "no data" instead of failing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ahub.i18n import t as _t


class Cap(StrEnum):
    RESUME = "resume"  # resume the same session
    STREAM = "stream"  # activity stream as work progresses
    STRUCTURED = "structured"  # structured result against a schema
    TOKENS = "tokens"  # per-session tokens
    COST_MONEY = "cost_money"  # money per session (go/usd)
    COST_QUOTA = "cost_quota"  # quota spend (window)
    ACTIVE_TOOL = "active_tool"  # which tool is running now (from provider data)
    EXPORT = "export"  # full session log
    CATALOG = "catalog"  # model/variant list
    HEALTH = "health"  # availability check
    FIND_SESSION = "find_session"  # find the session if the id was missed


class Outcome(StrEnum):
    OK = "ok"  # model finished its turn
    MODEL_ERROR = "model_error"  # model/request error (don't blindly retry)
    TRANSIENT = "transient"  # network/server failure — retry makes sense
    SILENCE = "silence"  # no activity past the threshold, nothing explains it — interrupted
    TIMEOUT = "timeout"  # overall step time limit
    QUOTA = "quota"  # quota/limit exhausted — wait for the window
    NO_ACCESS = "no_access"  # not authorized / no access to the model
    CRASH = "crash"  # process died with no result
    KILLED = "killed"  # stopped at the core's request
    NOT_STARTED = "not_started"  # never started (no binary, etc.)


RETRYABLE = frozenset({Outcome.TRANSIENT})


class Act(StrEnum):
    SESSION = "session"  # session id became known (text = id)
    STEP = "step"  # model step start/end
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TEXT = "text"  # reply text (final — the last one)
    REASONING = "reasoning"
    USAGE = "usage"  # step tokens/money (in data)
    ERROR = "error"  # error (data.transient, data.quota, data.no_access — provider classification)
    OTHER = "other"


@dataclass(frozen=True)
class Activity:
    kind: Act
    ts: int  # ms; when seen (or provider event time)
    text: str = ""
    tool: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class Usage:
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_reasoning: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    cost_go: float | None = None  # "list price" vs subscription
    cost_usd: float | None = None  # real money
    quota: float | None = None  # quota spend (provider units)
    context: int | None = None  # last turn context size

    def add(self, other: "Usage") -> "Usage":
        def s(a, b):
            return b if a is None else a if b is None else a + b

        return Usage(s(self.tokens_in, other.tokens_in), s(self.tokens_out, other.tokens_out),
                     s(self.tokens_reasoning, other.tokens_reasoning), s(self.cache_read, other.cache_read),
                     s(self.cache_write, other.cache_write), s(self.cost_go, other.cost_go),
                     s(self.cost_usd, other.cost_usd), s(self.quota, other.quota),
                     other.context if other.context is not None else self.context)


@dataclass
class RunSpec:
    prompt: str
    cwd: str
    model_id: str
    variant: str = ""
    session_id: str | None = None  # resume this session
    log_path: str = ""  # raw output goes here as it arrives
    timeout_s: int = 90 * 60
    idle_s: int = 900  # 0 — silence watchdog off
    schema: dict | None = None  # structured result, if the provider supports it
    env: dict[str, str] = field(default_factory=dict)  # added to the process environment


@dataclass
class RunResult:
    outcome: Outcome
    session_id: str | None
    final_text: str = ""
    structured: dict | None = None
    usage: Usage | None = None
    error: str = ""  # human-readable error/reason (≤ 2000)
    exit_code: int | None = None
    started_ms: int = 0
    ended_ms: int = 0
    log_path: str = ""
    activities: int = 0
    last_activity_ms: int = 0
    silence_s: int = 0  # for SILENCE — how long it stayed silent

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    variants: tuple[str, ...] = ()
    counter: str = "go"  # go | usd | quota | free
    price_in: float | None = None  # $ per 1M input
    price_out: float | None = None  # $ per 1M output
    note: str = ""


@dataclass(frozen=True)
class Health:
    ok: bool
    problems: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SessionState:
    """What the provider knows about a session from its own data (not the stream): for pulse and usage."""

    last_activity_ms: int | None = None
    active_tool: str = ""
    tool_started_ms: int | None = None
    finished: bool | None = None
    usage: Usage | None = None


class Provider(ABC):
    name: str = ""
    capabilities: frozenset[Cap] = frozenset()
    # The stream events carry their own timestamps (opencode). False — parse_line gets `now` and every
    # activity of a line has it; a reader that has to place activities in time (the transcript) splits
    # the stream by the session id each process run starts with instead.
    stamped: bool = False

    def has(self, cap: Cap) -> bool:
        return cap in self.capabilities

    # --- start ---

    @abstractmethod
    def build_command(self, spec: RunSpec) -> list[str]:
        """Process command (start or resume when spec.session_id is set)."""

    def env(self, spec: RunSpec) -> dict[str, str]:
        """Extra process environment (isolation). By default — spec.env only."""
        return dict(spec.env)

    @abstractmethod
    def parse_line(self, line: str, now: int) -> list[Activity]:
        """One stdout line → activities (may be empty). Never raises."""

    def classify(self, *, exit_code: int | None, activities: list[Activity], session_id: str | None,
                 stderr_tail: str) -> tuple[Outcome, str]:
        """Result of a finished process. By default — from ERROR events with classification in data.

        v1 rule: a network failure at rc=0 with a known id is transient, the step counts as done.
        """
        errors = [a for a in activities if a.kind is Act.ERROR]
        for flag, outcome in (("no_access", Outcome.NO_ACCESS), ("quota", Outcome.QUOTA)):
            hit = next((a for a in errors if a.data.get(flag)), None)
            if hit is not None:
                return outcome, hit.text[:2000]
        transient = next((a for a in errors if a.data.get("transient")), None)
        if transient is not None and not (exit_code == 0 and session_id):
            return Outcome.TRANSIENT, transient.text[:2000]
        if exit_code not in (0, None):
            last = errors[-1].text if errors else stderr_tail
            return Outcome.MODEL_ERROR if errors else Outcome.CRASH, \
                (last or _t("provider.exit_code", code=exit_code))[-2000:]
        if session_id is None and self.has(Cap.RESUME):
            return Outcome.CRASH, _t("provider.no_session", tail=stderr_tail)[-2000:]
        return Outcome.OK, ""

    def final_text(self, activities: list[Activity]) -> str:
        texts = [a.text for a in activities if a.kind is Act.TEXT and a.text]
        return texts[-1] if texts else ""

    def structured(self, final_text: str, activities: list[Activity], schema: dict | None) -> dict | None:
        return None

    def stream_usage(self, activities: list[Activity]) -> Usage | None:
        """Usage from stream events (if the provider sends them)."""
        acc: Usage | None = None
        for a in activities:
            if a.kind is Act.USAGE and isinstance(a.data.get("usage"), Usage):
                acc = a.data["usage"] if acc is None else acc.add(a.data["usage"])
        return acc

    # --- provider data ---

    def usage(self, session_id: str) -> Usage | None:
        return None

    def session_state(self, session_id: str) -> SessionState | None:
        return None

    def find_session(self, cwd: str, started_after_ms: int) -> str | None:
        return None

    def export(self, session_id: str) -> dict | None:
        return None

    def catalog(self) -> list[ModelInfo]:
        return []

    def health(self) -> Health:
        return Health(ok=True, problems=(_t("provider.no_health"),))


def clip(text: Any, limit: int = 2000) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[-limit:]
