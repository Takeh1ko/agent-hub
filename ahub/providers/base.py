"""Контракт поставщика моделей (architecture §3).

Поставщик — модуль, который умеет работать с одним источником моделей (opencode, agy, позже Codex…).
Общую механику процесса (поток, тишина, таймаут, остановка, лог) делает ahub.providers.runner — одинаково
для всех. Модуль поставщика отвечает только за своё:

- build_command / env — как запустить или продолжить сессию;
- parse_line — строка вывода → нормализованная активность (Activity), в т.ч. id сессии и ошибки;
- classify — итог процесса → Outcome (поставщик классифицирует, решает ядро);
- final_text / structured — последний ответ и структурированный итог;
- usage / session_state / find_session / export — данные поставщика о сессии (учёт, пульс, запасной id, журнал);
- catalog / health — модели и здоровье.

Каждый модуль объявляет `capabilities`. Чего нет — метод возвращает None, а ядро честно показывает
«нет данных», а не падает.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Cap(StrEnum):
    RESUME = "resume"  # продолжить ту же сессию
    STREAM = "stream"  # поток активности по ходу работы
    STRUCTURED = "structured"  # структурированный итог по схеме
    TOKENS = "tokens"  # токены по сессии
    COST_MONEY = "cost_money"  # деньги по сессии (go/usd)
    COST_QUOTA = "cost_quota"  # расход квоты (окно)
    ACTIVE_TOOL = "active_tool"  # какой инструмент идёт сейчас (из данных поставщика)
    EXPORT = "export"  # полный журнал сессии
    CATALOG = "catalog"  # список моделей/вариантов
    HEALTH = "health"  # проверка доступности
    FIND_SESSION = "find_session"  # найти сессию, если id не пойман


class Outcome(StrEnum):
    OK = "ok"  # модель закончила ход
    MODEL_ERROR = "model_error"  # ошибка модели/запроса (не повторять вслепую)
    TRANSIENT = "transient"  # сбой сети/сервера — повтор имеет смысл
    SILENCE = "silence"  # нет активности дольше порога и нечем объяснить — прервали
    TIMEOUT = "timeout"  # общий лимит времени шага
    QUOTA = "quota"  # квота/лимит исчерпан — ждать окна
    NO_ACCESS = "no_access"  # не авторизован / нет доступа к модели
    CRASH = "crash"  # процесс умер без результата
    KILLED = "killed"  # остановлен по просьбе ядра
    NOT_STARTED = "not_started"  # не запустился (нет бинаря и т.п.)


RETRYABLE = frozenset({Outcome.TRANSIENT})


class Act(StrEnum):
    SESSION = "session"  # стал известен id сессии (text = id)
    STEP = "step"  # начало/конец шага модели
    TOOL_START = "tool_start"
    TOOL_END = "tool_end"
    TEXT = "text"  # текст ответа (финальный — последний)
    REASONING = "reasoning"
    USAGE = "usage"  # токены/деньги шага (в data)
    ERROR = "error"  # ошибка (data.transient, data.quota, data.no_access — классификация поставщика)
    OTHER = "other"


@dataclass(frozen=True)
class Activity:
    kind: Act
    ts: int  # мс; когда увидели (или время события у поставщика)
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
    cost_go: float | None = None  # «по прайсу» против подписки
    cost_usd: float | None = None  # реальные деньги
    quota: float | None = None  # расход квоты (единицы поставщика)
    context: int | None = None  # размер контекста последнего хода

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
    session_id: str | None = None  # продолжить эту сессию
    log_path: str = ""  # сырой вывод пишется сюда по ходу
    timeout_s: int = 90 * 60
    idle_s: int = 900  # 0 — сторож тишины выключен
    schema: dict | None = None  # структурированный итог, если поставщик умеет
    env: dict[str, str] = field(default_factory=dict)  # добавить к окружению процесса


@dataclass
class RunResult:
    outcome: Outcome
    session_id: str | None
    final_text: str = ""
    structured: dict | None = None
    usage: Usage | None = None
    error: str = ""  # текст ошибки/причины для людей (≤ 2000)
    exit_code: int | None = None
    started_ms: int = 0
    ended_ms: int = 0
    log_path: str = ""
    activities: int = 0
    last_activity_ms: int = 0
    silence_s: int = 0  # для SILENCE — сколько молчал

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    variants: tuple[str, ...] = ()
    counter: str = "go"  # go | usd | quota | free
    price_in: float | None = None  # $ за 1M входных
    price_out: float | None = None  # $ за 1M выходных
    note: str = ""


@dataclass(frozen=True)
class Health:
    ok: bool
    problems: tuple[str, ...] = ()
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SessionState:
    """Что поставщик знает о сессии из своих данных (не из потока): для пульса и учёта."""

    last_activity_ms: int | None = None
    active_tool: str = ""
    tool_started_ms: int | None = None
    finished: bool | None = None
    usage: Usage | None = None


class Provider(ABC):
    name: str = ""
    capabilities: frozenset[Cap] = frozenset()

    def has(self, cap: Cap) -> bool:
        return cap in self.capabilities

    # --- запуск ---

    @abstractmethod
    def build_command(self, spec: RunSpec) -> list[str]:
        """Команда процесса (старт или продолжение, если spec.session_id)."""

    def env(self, spec: RunSpec) -> dict[str, str]:
        """Добавки к окружению процесса (изоляция). По умолчанию — только spec.env."""
        return dict(spec.env)

    @abstractmethod
    def parse_line(self, line: str, now: int) -> list[Activity]:
        """Одна строка stdout → активности (может быть пусто). Не бросает."""

    def classify(self, *, exit_code: int | None, activities: list[Activity], session_id: str | None,
                 stderr_tail: str) -> tuple[Outcome, str]:
        """Итог завершившегося процесса. По умолчанию — по событиям ERROR с классификацией в data.

        Правило v1: сбой сети при rc=0 и полученном id — промежуточный, шаг успешен.
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
            return Outcome.MODEL_ERROR if errors else Outcome.CRASH, (last or f"код выхода {exit_code}")[-2000:]
        if session_id is None and self.has(Cap.RESUME):
            return Outcome.CRASH, ("нет id сессии в выводе; " + stderr_tail)[-2000:]
        return Outcome.OK, ""

    def final_text(self, activities: list[Activity]) -> str:
        texts = [a.text for a in activities if a.kind is Act.TEXT and a.text]
        return texts[-1] if texts else ""

    def structured(self, final_text: str, activities: list[Activity], schema: dict | None) -> dict | None:
        return None

    def stream_usage(self, activities: list[Activity]) -> Usage | None:
        """Учёт по событиям потока (если поставщик их шлёт)."""
        acc: Usage | None = None
        for a in activities:
            if a.kind is Act.USAGE and isinstance(a.data.get("usage"), Usage):
                acc = a.data["usage"] if acc is None else acc.add(a.data["usage"])
        return acc

    # --- данные поставщика ---

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
        return Health(ok=True, problems=("проверка здоровья не поддерживается",))


def clip(text: Any, limit: int = 2000) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[-limit:]
