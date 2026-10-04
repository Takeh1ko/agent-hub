"""Task model at creation: fields, per-kind defaults, pre-launch validation (architecture §5).

An invalid task is never created — no paid model call. Checks (v1 lint ported to
structured fields): allowed files ⊆ project allow-list, "read first" files exist, acceptance
collects via pytest (new acceptance files are fine if within allowed paths), models available to the project,
dependencies exist, resources declared by the project.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ahub import registry
from ahub.config import ProjectConfig
from ahub.i18n import t as _t
from ahub.model import ROLE_FOR_KIND, Kind, Role, State
from ahub.store import Store, Task

MAX_TITLE = 200
MAX_SPEC_BYTES = 40_000
MAX_ROUNDS = 5

# Review levels (owner decision 2026-09-30, as in v1): level → (models, rounds).
REVIEW_LEVELS: dict[int, tuple[tuple[str, ...], int]] = {
    1: (("spark",), 1),
    2: (("spark",), 2),
    3: (("spark", "mimo-flash"), 1),
    4: (("spark", "mimo-flash"), 2),
}

# Per-kind defaults: (review level, 0 — no review; time limit, min).
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
    """What the orchestrator or human sets. None — take the default."""

    project: str
    kind: Kind
    title: str
    spec: str = ""
    result_format: str = ""
    model: str | None = None
    review_level: int | None = None  # 0 — no review; 1–4 — levels
    review_models: list[str] | None = None  # explicit roster (overrides level)
    review_rounds: int | None = None
    paths: list[str] = field(default_factory=list)  # allowed files (glob)
    accept: list[str] = field(default_factory=list)  # acceptance pytest nodes
    read: list[str] = field(default_factory=list)  # what to read first
    review_input: str = ""  # for the "review" kind: branch, sha, a..b range, or files
    resources: list[str] = field(default_factory=list)
    after: list[int] = field(default_factory=list)
    budget_go: float | None = None
    budget_usd: float | None = None
    time_limit_min: int | None = None
    created_by: str = "orchestrator"


@dataclass(frozen=True)
class Resolved:
    """Task after defaults and validation — ready to store."""

    executor: str
    review: dict
    limits: dict
    budget_go: float
    budget_usd: float
    spec_hash: str


def spec_hash(spec: TaskSpec) -> str:
    """Brief fingerprint: a change continues in a new session."""
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
    """pytest --collect-only at the project root; error text or None."""
    py = project.python_bin()
    if not Path(py).exists():
        return _t("tasks.no_python", py=py)
    try:
        r = subprocess.run([py, "-m", "pytest", "--collect-only", "-q", *nodes], cwd=project.root,
                           capture_output=True, text=True, timeout=180)
    except OSError as e:
        return _t("tasks.pytest_start", err=e)
    except subprocess.TimeoutExpired:
        return _t("tasks.collect_timeout")
    if r.returncode != 0:
        tail = (r.stdout + "\n" + r.stderr).strip()
        last = tail.splitlines()[-1] if tail else _t("err.exit_code", code=r.returncode)
        return _t("tasks.collect_failed", nodes=", ".join(nodes), last=last[-300:])
    return None


def resolve(store: Store, spec: TaskSpec, project: ProjectConfig, *, collect: bool = True) -> Resolved:
    """Defaults + all checks. Errors — batched, in TaskInvalid."""
    errors: list[str] = []
    kind = Kind(spec.kind)
    if spec.project != project.name:
        errors.append(_t("tasks.project_mismatch", spec=spec.project, name=project.name))
    title = spec.title.strip()
    if not title:
        errors.append(_t("tasks.need_title"))
    elif len(title) > MAX_TITLE:
        errors.append(_t("tasks.title_long", max=MAX_TITLE))
    if len(spec.spec.encode("utf-8")) > MAX_SPEC_BYTES:
        errors.append(_t("tasks.spec_big", kb=MAX_SPEC_BYTES // 1000))

    # Models
    role = ROLE_FOR_KIND[kind]
    executor = ""
    try:
        executor = registry.pick(store, role, project, spec.model).alias
    except registry.RegistryError as e:
        errors.append(str(e))
    # The review kind is not reviewed itself: --review names its panel, --model one reviewer — not both,
    # and the panel of a review task reviews once, so --rounds is nothing for it.
    if kind is Kind.REVIEW:
        if spec.review_models and spec.model:
            errors.append(_t("tasks.review_both"))
        if spec.review_rounds is not None:
            errors.append(_t("tasks.review_no_rounds"))
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
            errors.append(_t("tasks.bad_level", level=level))
            rmodels, rounds = [], 0
        if spec.review_rounds is not None and rmodels:
            rounds = spec.review_rounds
    if rmodels and not 1 <= rounds <= MAX_ROUNDS:
        errors.append(_t("tasks.bad_rounds", rounds=rounds, max=MAX_ROUNDS))
    for m in rmodels:
        try:
            registry.check(store, m, project)
        except registry.RegistryError as e:
            errors.append(_t("tasks.review_prefix", err=e))

    # Files and acceptance
    paths = [_norm(p) for p in spec.paths if p.strip()]
    if kind in (Kind.CODE, Kind.ROUTINE) and not paths:
        errors.append(_t("tasks.need_paths"))
    if kind in (Kind.SCOUT, Kind.REVIEW) and paths:
        errors.append(_t("tasks.no_paths", kind=kind.value))
    for p in paths:
        if _path_escapes(p) or not _glob_allowed(p, project.allowed_paths):
            errors.append(_t("tasks.path_outside", path=p,
                             allowed=", ".join(project.allowed_paths) or "—"))
    root = Path(project.root)
    for p in spec.read:
        if _path_escapes(p) or not (root / p).exists():
            errors.append(_t("tasks.no_read", path=p))
    accept = [a.strip() for a in spec.accept if a.strip()]
    if kind is Kind.CODE and not accept:
        errors.append(_t("tasks.need_accept"))
    existing = []
    for node in accept:
        file = node.split("::")[0]
        if _path_escapes(file):
            errors.append(_t("tasks.accept_outside", node=node))
        elif (root / file).exists():
            existing.append(node)
        elif not _covered(file, paths):
            errors.append(_t("tasks.no_accept_file", file=file))
    if collect and existing and not errors:
        err = _collect(existing, project)
        if err:
            errors.append(err)
    if kind is Kind.REVIEW and not spec.review_input.strip():
        errors.append(_t("tasks.need_input"))

    # Dependencies, resources, limits
    for a in spec.after:
        dep = store.get_task(a)
        if dep is None:
            errors.append(_t("tasks.no_dep", id=a))
        elif dep.project != project.name:
            errors.append(_t("tasks.dep_other", id=a, project=dep.project))
        elif dep.state is State.REJECTED:
            errors.append(_t("tasks.dep_rejected", id=a))
    for r in spec.resources:
        if r not in project.resources:
            errors.append(_t("tasks.no_resource", name=r))
    # The project test resource is not added here: acceptance takes its lock itself (gates), so the queue
    # does not hold it and code tasks run in parallel. Name it in --resources for whole-task exclusivity.
    resources = list(dict.fromkeys(spec.resources))
    budget_go = project.budget_go if spec.budget_go is None else spec.budget_go
    budget_usd = project.budget_usd if spec.budget_usd is None else spec.budget_usd
    if budget_go < 0 or budget_usd < 0:
        errors.append(_t("tasks.bad_budget"))
    tlim = time_default if spec.time_limit_min is None else spec.time_limit_min
    if tlim <= 0:
        errors.append(_t("tasks.bad_time"))

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
    """Validate and store a task (queued or draft). key — idempotent command retry."""
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
