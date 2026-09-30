"""Модель задачи при создании: поля, умолчания по типу, проверка перед запуском (architecture §5).

Задача, не прошедшая проверку, не создаётся — модель за деньги не зовётся. Проверки (перенос lint v1 под
структурированные поля): разрешённые файлы ⊆ разрешённых проекту, файлы «прочитать» существуют, приёмка
собирается pytest'ом (новые файлы приёмки допустимы, если попадают в разрешённые), модели доступны проекту,
зависимости существуют, ресурсы объявлены проектом.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ahub import registry
from ahub.config import ProjectConfig
from ahub.model import ROLE_FOR_KIND, Kind, Role, State
from ahub.store import Store, Task

MAX_TITLE = 200
MAX_SPEC_BYTES = 40_000
MAX_ROUNDS = 5

# Уровни ревью (решение владельца 2026-09-30, как в v1): уровень → (модели, круги).
REVIEW_LEVELS: dict[int, tuple[tuple[str, ...], int]] = {
    1: (("spark",), 1),
    2: (("spark",), 2),
    3: (("spark", "mimo-flash"), 1),
    4: (("spark", "mimo-flash"), 2),
}

# Умолчания по типу: (уровень ревью или 0 — без ревью, лимит времени мин).
KIND_DEFAULTS: dict[Kind, tuple[int, int]] = {
    Kind.SCOUT: (0, 60),
    Kind.CODE: (2, 180),
    Kind.ROUTINE: (1, 60),
    Kind.REVIEW: (0, 60),
}


class TaskInvalid(ValueError):
    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@dataclass
class TaskSpec:
    """Что задаёт оркестратор или человек. None — взять умолчание."""

    project: str
    kind: Kind
    title: str
    spec: str = ""
    result_format: str = ""
    model: str | None = None
    review_level: int | None = None  # 0 — без ревью; 1–4 — уровни
    review_models: list[str] | None = None  # явный состав (сильнее уровня)
    review_rounds: int | None = None
    paths: list[str] = field(default_factory=list)  # разрешённые файлы (glob)
    accept: list[str] = field(default_factory=list)  # pytest-ноды приёмки
    read: list[str] = field(default_factory=list)  # что прочитать первым
    review_input: str = ""  # для типа «ревью»: ветка, sha, диапазон a..b или файлы
    resources: list[str] = field(default_factory=list)
    after: list[int] = field(default_factory=list)
    budget_go: float | None = None
    budget_usd: float | None = None
    time_limit_min: int | None = None
    created_by: str = "orchestrator"


@dataclass(frozen=True)
class Resolved:
    """Задача после умолчаний и проверки — готова к записи."""

    executor: str
    review: dict
    limits: dict
    budget_go: float
    budget_usd: float
    spec_hash: str


def spec_hash(spec: TaskSpec) -> str:
    """Отпечаток постановки: меняется — продолжение пойдёт новой сессией."""
    body = json.dumps({"t": spec.title, "s": spec.spec, "f": spec.result_format, "p": sorted(spec.paths),
                       "a": sorted(spec.accept), "i": spec.review_input}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _norm(p: str) -> str:
    return p.strip().removeprefix("./")


def _path_escapes(p: str) -> bool:
    pp = Path(p)
    return pp.is_absolute() or ".." in pp.parts


def _glob_allowed(glob: str, allowed: tuple[str, ...]) -> bool:
    if not allowed:
        return True
    g = _norm(glob)
    return any(fnmatch.fnmatch(g, pat) or g == pat for pat in allowed)


def _covered(file: str, globs: list[str]) -> bool:
    f = _norm(file)
    return any(fnmatch.fnmatch(f, _norm(g)) for g in globs)


def _collect(nodes: list[str], project: ProjectConfig) -> str | None:
    """pytest --collect-only в корне проекта; текст ошибки или None."""
    py = project.python or sys.executable
    if not Path(py).exists():
        return f"питон проекта не найден: {py}"
    try:
        r = subprocess.run([py, "-m", "pytest", "--collect-only", "-q", *nodes], cwd=project.root,
                           capture_output=True, text=True, timeout=180)
    except OSError as e:
        return f"pytest не запустился: {e}"
    except subprocess.TimeoutExpired:
        return "pytest --collect-only: таймаут"
    if r.returncode != 0:
        tail = (r.stdout + "\n" + r.stderr).strip()
        last = tail.splitlines()[-1] if tail else f"код {r.returncode}"
        return f"приёмка не собирается ({', '.join(nodes)}): {last[-300:]}"
    return None


def resolve(store: Store, spec: TaskSpec, project: ProjectConfig, *, collect: bool = True) -> Resolved:
    """Умолчания + все проверки. Ошибки — разом, в TaskInvalid."""
    errors: list[str] = []
    kind = Kind(spec.kind)
    if spec.project != project.name:
        errors.append(f"задача проекта {spec.project!r}, а конфиг — {project.name!r}")
    title = spec.title.strip()
    if not title:
        errors.append("нужна цель (title)")
    elif len(title) > MAX_TITLE:
        errors.append(f"цель длиннее {MAX_TITLE} символов — подробности в описание")
    if len(spec.spec.encode("utf-8")) > MAX_SPEC_BYTES:
        errors.append(f"описание больше {MAX_SPEC_BYTES // 1000} КБ")

    # Модели
    role = ROLE_FOR_KIND[kind]
    executor = ""
    try:
        executor = registry.pick(store, role, project, spec.model).alias
    except registry.RegistryError as e:
        errors.append(str(e))
    level_default, time_default = KIND_DEFAULTS[kind]
    if spec.review_models is not None:
        rmodels, rounds = list(spec.review_models), spec.review_rounds or (1 if spec.review_models else 0)
    else:
        level = level_default if spec.review_level is None else spec.review_level
        if level == 0:
            rmodels, rounds = [], 0
        elif level in REVIEW_LEVELS:
            rmodels, rounds = list(REVIEW_LEVELS[level][0]), REVIEW_LEVELS[level][1]
        else:
            errors.append(f"уровень ревью {level}: допустимо 0–4")
            rmodels, rounds = [], 0
        if spec.review_rounds is not None and rmodels:
            rounds = spec.review_rounds
    if rmodels and not 1 <= rounds <= MAX_ROUNDS:
        errors.append(f"кругов ревью {rounds}: допустимо 1–{MAX_ROUNDS}")
    for m in rmodels:
        try:
            registry.check(store, m, project)
        except registry.RegistryError as e:
            errors.append(f"ревью: {e}")
    if kind is Kind.REVIEW and rmodels:
        errors.append("задача «ревью» сама не ревьюится (ревью = 0)")

    # Файлы и приёмка
    paths = [_norm(p) for p in spec.paths if p.strip()]
    if kind in (Kind.CODE, Kind.ROUTINE) and not paths:
        errors.append("нужны разрешённые файлы (--paths) для задачи, меняющей файлы")
    if kind in (Kind.SCOUT, Kind.REVIEW) and paths:
        errors.append(f"задача «{kind.value}» файлы не меняет — --paths не нужен")
    for p in paths:
        if _path_escapes(p) or not _glob_allowed(p, project.allowed_paths):
            errors.append(f"файлы «{p}» вне разрешённых проекту ({', '.join(project.allowed_paths) or '—'})")
    root = Path(project.root)
    for p in spec.read:
        if _path_escapes(p) or not (root / p).exists():
            errors.append(f"нет файла для чтения «{p}»")
    accept = [a.strip() for a in spec.accept if a.strip()]
    if kind is Kind.CODE and not accept:
        errors.append("нужна приёмка (--accept pytest-ноды) для задачи «код»")
    existing = []
    for node in accept:
        file = node.split("::")[0]
        if _path_escapes(file):
            errors.append(f"приёмка вне проекта: {node}")
        elif (root / file).exists():
            existing.append(node)
        elif not _covered(file, paths):
            errors.append(f"нет файла приёмки «{file}» и он не в разрешённых файлах")
    if collect and existing and not errors:
        err = _collect(existing, project)
        if err:
            errors.append(err)
    if kind is Kind.REVIEW and not spec.review_input.strip():
        errors.append("для задачи «ревью» нужен вход (--input: ветка, sha, a..b или файлы)")

    # Зависимости, ресурсы, лимиты
    for a in spec.after:
        dep = store.get_task(a)
        if dep is None:
            errors.append(f"нет задачи T{a} (после)")
        elif dep.project != project.name:
            errors.append(f"T{a} из другого проекта ({dep.project})")
        elif dep.state is State.REJECTED:
            errors.append(f"T{a} отклонена — ждать нечего")
    for r in spec.resources:
        if r not in project.resources:
            errors.append(f"ресурс {r!r} не объявлен в .hub.toml проекта")
    resources = list(dict.fromkeys(spec.resources))
    if kind is Kind.CODE and project.test_resource and project.test_resource not in resources:
        resources.append(project.test_resource)  # приёмка идёт под ресурсом тестов
    budget_go = project.budget_go if spec.budget_go is None else spec.budget_go
    budget_usd = project.budget_usd if spec.budget_usd is None else spec.budget_usd
    if budget_go < 0 or budget_usd < 0:
        errors.append("бюджет не может быть отрицательным")
    tlim = time_default if spec.time_limit_min is None else spec.time_limit_min
    if tlim <= 0:
        errors.append("лимит времени должен быть > 0")

    if errors:
        raise TaskInvalid(errors)
    limits = {"paths": paths, "accept": accept, "read": list(spec.read), "resources": resources,
              "time_limit_min": tlim}
    if spec.review_input.strip():
        limits["input"] = spec.review_input.strip()
    review = {"models": rmodels, "rounds": rounds} if rmodels else {}
    return Resolved(executor, review, limits, float(budget_go), float(budget_usd), spec_hash(spec))


def create(store: Store, spec: TaskSpec, project: ProjectConfig, *, key: str | None = None,
           draft: bool = False, collect: bool = True) -> Task:
    """Проверить и записать задачу (queued или draft). key — идемпотентность повтора команды."""
    from ahub import transitions

    res = resolve(store, spec, project, collect=collect)

    def _do(con) -> dict:
        tid = store.create_task(project=project.name, kind=spec.kind, title=spec.title.strip(), spec=spec.spec,
                                spec_hash=res.spec_hash, result_format=spec.result_format, executor=res.executor,
                                review=res.review, limits=res.limits, budget_go=res.budget_go,
                                budget_usd=res.budget_usd, state=State.DRAFT if draft else State.QUEUED,
                                created_by=spec.created_by, after=spec.after, con=con)
        return {"id": tid}

    if key:
        out = transitions.once(store, f"task-new:{project.name}:{key}", _do)
    else:
        with store.tx() as c:
            out = _do(c)
    task = store.get_task(int(out["id"]))
    assert task is not None
    return task


def role_of(task: Task) -> Role:
    return ROLE_FOR_KIND[task.kind]
