"""Модель задачи: типы, состояния, допустимые переходы, виды событий, роли.

Единственный источник этих имён для всех частей хаба (хранилище, движок, ручки, экраны).
Слова для человека — в ahub/human.py (позже), здесь только машинные имена и смысл.
"""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
    SCOUT = "scout"  # разведка: читает, пишет отчёт, файлы не меняет
    CODE = "code"  # код: своя копия и ветка, строгие ворота, слияние
    REVIEW = "review"  # ревью явно указанного входа: замечания
    ROUTINE = "routine"  # рутина: изменения файлов с облегчёнными воротами, слияние


CHANGES_FILES = frozenset({Kind.CODE, Kind.ROUTINE})


class State(StrEnum):
    DRAFT = "draft"  # черновик: ждёт явного «Запустить»
    QUEUED = "queued"  # ждёт места/ресурса/зависимости
    PREPARING = "preparing"  # копия проекта, ветка, окружение
    WORKING = "working"  # работник делает задачу (фаза уточняет: изучает/пишет)
    CHECKING = "checking"  # ворота
    REVIEWING = "reviewing"  # панель ревью
    FIXING = "fixing"  # доработка по замечаниям (та же сессия исполнителя)
    DONE = "done"  # готово — ждёт решения оркестратора/человека
    NEEDS_DECISION = "needs_decision"  # круги кончились, бюджет, спорное — нужно решение
    ERROR = "error"  # сбой, который хаб сам не исправит
    STOPPED = "stopped"  # остановлена командой
    ACCEPTING = "accepting"  # идёт принятие (слияние + повтор приёмки)
    ACCEPTED = "accepted"  # принята: слита / отчёт принят
    REJECTED = "rejected"  # отклонена / отменена


# Задача в работе у процесса задачи (у неё есть владелец и пульс).
ACTIVE = frozenset({State.PREPARING, State.WORKING, State.CHECKING, State.REVIEWING, State.FIXING,
                    State.ACCEPTING})
# Ждёт человека/оркестратора; можно продолжить или решить.
WAITING_DECISION = frozenset({State.DONE, State.NEEDS_DECISION, State.ERROR, State.STOPPED})
FINAL = frozenset({State.ACCEPTED, State.REJECTED})

_ORPHAN = State.QUEUED  # возврат сироты в очередь (из любого активного, кроме принятия)

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
    """Что видно в активной задаче (по активности работника и этапу движка)."""

    NONE = ""
    STUDYING = "studying"  # читает, ищет
    WRITING = "writing"  # пишет код/отчёт
    TESTING = "testing"  # тесты
    WAITING = "waiting"  # ждёт по делу: замок, ресурс, окно квоты, пауза перед повтором


class Role(StrEnum):
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    SCOUT = "scout"
    ROUTINE = "routine"
    OBSERVER = "observer"
    DRAFTER = "drafter"  # пишет черновик задачи из текста человека


ROLE_FOR_KIND: dict[Kind, Role] = {
    Kind.SCOUT: Role.SCOUT,
    Kind.CODE: Role.EXECUTOR,
    Kind.REVIEW: Role.REVIEWER,
    Kind.ROUTINE: Role.ROUTINE,
}


class Ev(StrEnum):
    """Виды событий журнала. Требующие реакции оркестратора — в NEEDS_REACTION."""

    CREATED = "created"
    STATE = "state"  # смена состояния: payload {from, to, reason}
    PHASE = "phase"
    SESSION = "session"  # сессия работника начата/продолжена: {role, model, session_id}
    RETRY = "retry"  # повтор после сбоя поставщика: {reason, attempt, pause_s}
    SILENCE = "silence"  # работник молчал: {secs, action}
    BUDGET_SOFT = "budget_soft"  # 80 %: в журнал, не будит
    BUDGET_HARD = "budget_hard"
    ORPHAN = "orphan"  # задача без процесса возвращена в очередь
    ORCH_EDIT = "orch_edit"  # правка оркестратора поверх результата
    PATHS_EXTENDED = "paths_extended"  # оркестратор расширил разрешённые файлы
    MODEL_CHANGED = "model_changed"
    BUDGET_EXTENDED = "budget_extended"
    # требуют реакции оркестратора:
    DONE = "done"
    NEEDS_DECISION = "needs_decision"
    ERROR = "error"
    OWNER_MESSAGE = "owner_message"  # человек написал (TG)
    ANSWER = "answer"  # человек ответил на вопрос
    ALARM = "alarm"  # тревога наблюдателя


NEEDS_REACTION = frozenset({Ev.DONE, Ev.NEEDS_DECISION, Ev.ERROR, Ev.OWNER_MESSAGE, Ev.ANSWER, Ev.ALARM})

# Событие, которое хаб пишет при переходе в состояние (кроме общего STATE).
STATE_EVENT: dict[State, Ev] = {
    State.DONE: Ev.DONE,
    State.NEEDS_DECISION: Ev.NEEDS_DECISION,
    State.ERROR: Ev.ERROR,
}


def task_label(task_id: int) -> str:
    """Короткое имя задачи для людей и оркестратора: T12."""
    return f"T{task_id}"


def parse_task_id(text: str | int) -> int:
    """«T12», «t12», «12», 12 → 12."""
    if isinstance(text, int):
        return text
    s = str(text).strip()
    if s[:1] in ("T", "t"):
        s = s[1:]
    if not s.isdigit():
        raise ValueError(f"не номер задачи: {text!r}")
    return int(s)
