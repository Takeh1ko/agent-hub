"""Task model: kinds, states, allowed transitions, event kinds, roles.

Single source of these names for all hub parts (store, engine, handles, screens).
Human words live in ahub/human.py (later); only machine names and meaning here.
"""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
    SCOUT = "scout"  # scout: reads, writes a report, never changes files
    CODE = "code"  # code: own copy and branch, strict gates, merge
    REVIEW = "review"  # review of the given input: findings
    ROUTINE = "routine"  # routine: file changes with light gates, merge


CHANGES_FILES = frozenset({Kind.CODE, Kind.ROUTINE})


class State(StrEnum):
    DRAFT = "draft"  # draft: waits for an explicit "Start"
    QUEUED = "queued"  # waits for room/resource/dependency
    PREPARING = "preparing"  # project copy, branch, environment
    WORKING = "working"  # worker does the task (phase says: studying/writing)
    CHECKING = "checking"  # gates
    REVIEWING = "reviewing"  # review panel
    FIXING = "fixing"  # rework on findings (same executor session)
    DONE = "done"  # done — waits for the orchestrator/human decision
    NEEDS_DECISION = "needs_decision"  # rounds over, budget, disputed — needs a decision
    ERROR = "error"  # failure the hub can't fix itself
    STOPPED = "stopped"  # stopped by command
    ACCEPTING = "accepting"  # accepting in progress (merge + acceptance rerun)
    ACCEPTED = "accepted"  # accepted: merged / report accepted
    REJECTED = "rejected"  # rejected / cancelled


# Task owned by a task process (has an owner and a pulse).
ACTIVE = frozenset({State.PREPARING, State.WORKING, State.CHECKING, State.REVIEWING, State.FIXING,
                    State.ACCEPTING})
# The task process runs the worker here — a message from the orchestrator (nudge) can be delivered.
NUDGEABLE = ACTIVE - {State.ACCEPTING}
# Waits for a human/orchestrator; can be resumed or decided.
WAITING_DECISION = frozenset({State.DONE, State.NEEDS_DECISION, State.ERROR, State.STOPPED})
FINAL = frozenset({State.ACCEPTED, State.REJECTED})

_ORPHAN = State.QUEUED  # orphan back to queue (from any active state except accepting)

TRANSITIONS: dict[State, frozenset[State]] = {
    State.DRAFT: frozenset({State.QUEUED, State.REJECTED}),
    State.QUEUED: frozenset({State.PREPARING, State.STOPPED, State.REJECTED, State.NEEDS_DECISION}),
    State.PREPARING: frozenset({State.WORKING, State.FIXING, State.ERROR, State.STOPPED, State.NEEDS_DECISION,
                                _ORPHAN}),
    State.WORKING: frozenset({State.CHECKING, State.DONE, State.NEEDS_DECISION, State.ERROR,
                              State.STOPPED, _ORPHAN}),
    State.CHECKING: frozenset({State.WORKING, State.FIXING, State.REVIEWING, State.DONE,
                               State.NEEDS_DECISION, State.ERROR, State.STOPPED, _ORPHAN}),
    State.REVIEWING: frozenset({State.FIXING, State.DONE, State.NEEDS_DECISION, State.ERROR,
                                State.STOPPED, _ORPHAN}),
    State.FIXING: frozenset({State.CHECKING, State.NEEDS_DECISION, State.ERROR, State.STOPPED, _ORPHAN}),
    State.DONE: frozenset({State.ACCEPTING, State.ACCEPTED, State.REJECTED, State.QUEUED}),
    State.NEEDS_DECISION: frozenset({State.QUEUED, State.ACCEPTING, State.ACCEPTED, State.REJECTED,
                                     State.STOPPED}),
    State.ERROR: frozenset({State.QUEUED, State.REJECTED}),
    State.STOPPED: frozenset({State.QUEUED, State.REJECTED}),
    State.ACCEPTING: frozenset({State.ACCEPTED, State.DONE, State.NEEDS_DECISION}),
    State.ACCEPTED: frozenset(),
    State.REJECTED: frozenset(),
}


def can_move(src: State | str, dst: State | str) -> bool:
    return State(dst) in TRANSITIONS[State(src)]


class Phase(StrEnum):
    """What an active task shows (from worker activity and engine stage)."""

    NONE = ""
    STUDYING = "studying"  # reads, searches
    WRITING = "writing"  # writes code/report
    TESTING = "testing"  # tests
    WAITING = "waiting"  # waits for cause: lock, resource, quota window, pause before retry


class Role(StrEnum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    SCOUT = "scout"
    ROUTINE = "routine"
    OBSERVER = "observer"
    DRAFTER = "drafter"  # drafts a task from human text


ROLE_FOR_KIND: dict[Kind, Role] = {
    Kind.SCOUT: Role.SCOUT,
    Kind.CODE: Role.EXECUTOR,
    Kind.REVIEW: Role.REVIEWER,
    Kind.ROUTINE: Role.ROUTINE,
}


class Ev(StrEnum):
    """Log event kinds. The ones needing an orchestrator live in NEEDS_REACTION."""

    CREATED = "created"
    STATE = "state"  # state change: payload {from, to, reason}
    PHASE = "phase"
    SESSION = "session"  # worker session started/resumed: {role, model, session_id}
    RETRY = "retry"  # retry after a provider failure: {reason, attempt, pause_s}
    SILENCE = "silence"  # worker stayed silent: {secs, action}
    NUDGE = "nudge"  # message from the orchestrator into the worker's session: {text, by}
    BUDGET_SOFT = "budget_soft"  # 80 %: to the log, doesn't wake
    BUDGET_HARD = "budget_hard"
    ORPHAN = "orphan"  # task without a process returned to queue
    ORCH_EDIT = "orch_edit"  # orchestrator edit over the result
    PATHS_EXTENDED = "paths_extended"  # orchestrator widened the allowed files
    MODEL_CHANGED = "model_changed"
    BUDGET_EXTENDED = "budget_extended"
    # need an orchestrator reaction:
    DONE = "done"
    NEEDS_DECISION = "needs_decision"
    ERROR = "error"
    OWNER_MESSAGE = "owner_message"  # human wrote (TG)
    ANSWER = "answer"  # human answered a question
    ALARM = "alarm"  # observer alarm


NEEDS_REACTION = frozenset({Ev.DONE, Ev.NEEDS_DECISION, Ev.ERROR, Ev.OWNER_MESSAGE, Ev.ANSWER, Ev.ALARM})

# Event the hub writes on entering a state (besides the generic STATE).
STATE_EVENT: dict[State, Ev] = {
    State.DONE: Ev.DONE,
    State.NEEDS_DECISION: Ev.NEEDS_DECISION,
    State.ERROR: Ev.ERROR,
}


def task_label(task_id: int) -> str:
    """Short task name for humans and the orchestrator: T12."""
    return f"T{task_id}"


def parse_task_id(text: str | int) -> int:
    """"T12", "t12", "12", 12 → 12."""
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if s[:1] in ("T", "t"):
        s = s[1:]
    if not s.isdigit():
        raise ValueError(f"not a task number: {text!r}")
    return int(s)
