"""Черновик задачи (architecture §5): человек пишет цель своими словами → модель (роль drafter) дописывает поля →
проверка → предпросмотр → человек правит/запускает. Без явного «Запустить» задача не стартует.

Модель работает в отдельной копии проекта (только читает), итог — JSON с полями TaskSpec в `.ahub/draft.json`.
Проверка — tasks.resolve; ошибки → один повтор с их текстом, иначе черновик «failed».
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ahub import log as hublog
from ahub import providers, registry, tasks, workspace
from ahub.config import ProjectConfig
from ahub.model import Kind, Role
from ahub.prompts import rules_text
from ahub.providers.base import RunSpec
from ahub.providers.opencode import extract_json
from ahub.providers.runner import run as run_session
from ahub.store import Store
from ahub.time import now_ms

_log = hublog.get("drafts")
FIELDS = ("kind", "title", "spec", "result_format", "paths", "accept", "read", "review_level", "resources")

PROMPT = """Ты помогаешь владельцу (не программисту) поставить задачу моделям-работникам agent-hub. Прочитай код проекта
(ничего не меняй) и составь задачу по его словам.

## Слова владельца
{text}

## Как выбрать тип
- scout — разобраться/найти/объяснить, код не меняется;
- code — изменить код (обязательно тесты приёмки: pytest-ноды, можно новые файлы тестов);
- routine — навести порядок в файлах/документах без логики (без тестов);
- review — проверить уже написанный код (нужен вход: ветка/коммит/файлы — укажи в spec).

## Разрешённые проекту файлы
{allowed}

## Ответ
Запиши `.ahub/draft.json` и продублируй его последним сообщением:
{{"kind": "scout|code|routine|review", "title": "цель одной фразой (≤ 120 симв.)",
  "spec": "подробно: что сделать, где, как проверить, чего не трогать",
  "result_format": "какой нужен результат (для разведки — что должно быть в отчёте)",
  "paths": ["glob разрешённых файлов (для code/routine)"], "accept": ["pytest-ноды (для code)"],
  "read": ["что работнику прочитать первым"], "review_level": 0}}
review_level: 0 — без ревью, 1 — обычные документы/рутина, 2 — обычный код, 3–4 — код рядом с деньгами.
{errors}"""


def _row(store: Store, draft_id: int) -> dict | None:
    with store.read() as c:
        r = c.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
    return dict(r) if r else None


def _update(store: Store, draft_id: int, **fields) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    with store.tx() as c:
        c.execute(f"UPDATE draft SET {sets} WHERE id=?", (*fields.values(), draft_id))


def to_spec(project: ProjectConfig, data: dict, created_by: str = "human") -> tasks.TaskSpec:
    kind = Kind(str(data.get("kind", "scout")))
    level = data.get("review_level")
    return tasks.TaskSpec(project=project.name, kind=kind, title=str(data.get("title", "")).strip()[:200],
                          spec=str(data.get("spec", "")), result_format=str(data.get("result_format", "")),
                          paths=[str(x) for x in data.get("paths") or []] if kind in (Kind.CODE, Kind.ROUTINE) else [],
                          accept=[str(x) for x in data.get("accept") or []] if kind is Kind.CODE else [],
                          read=[str(x) for x in data.get("read") or [] if (Path(project.root) / str(x)).exists()],
                          review_level=int(level) if isinstance(level, int) and 0 <= level <= 4 else None,
                          resources=[str(x) for x in data.get("resources") or []], created_by=created_by)


def create(store: Store, project: ProjectConfig, text: str, *, source: str = "top", run_model: bool = True) -> int:
    with store.tx() as c:
        did = int(c.execute("INSERT INTO draft(ts, project, text, source) VALUES(?,?,?,?)",
                            (now_ms(), project.name, text, source)).lastrowid)
    if run_model:
        draft_with_model(store, project, did)
    return did


def draft_with_model(store: Store, project: ProjectConfig, draft_id: int) -> dict:
    row = _row(store, draft_id)
    assert row is not None
    try:
        entry = registry.pick(store, Role.DRAFTER, project)
    except registry.RegistryError as e:
        _update(store, draft_id, status="failed", errors=str(e))
        return _row(store, draft_id) or {}
    ws = _draft_copy(project, draft_id)
    errors = ""
    for attempt in (1, 2):
        prompt = "\n\n".join([rules_text(project).strip(), PROMPT.format(
            text=row["text"], allowed=", ".join(project.allowed_paths) or "любые",
            errors=f"\n## Прошлая попытка не прошла проверку\n{errors}\nИсправь." if errors else "")])
        r = run_session(providers.get(entry.provider), RunSpec(
            prompt=prompt, cwd=ws, model_id=entry.model_id, variant=entry.variant,
            log_path=str(Path(ws) / ".ahub" / f"draft_{attempt}.log"), timeout_s=15 * 60, idle_s=600))
        data = _read_draft(ws) or extract_json(r.final_text) or {}
        if not data:
            errors = f"нет JSON черновика ({r.outcome.value}: {r.error[:200]})"
            continue
        try:
            spec = to_spec(project, data)
            tasks.resolve(store, spec, project, collect=True)
        except (tasks.TaskInvalid, ValueError) as e:
            errors = "; ".join(e.errors) if isinstance(e, tasks.TaskInvalid) else str(e)
            continue
        _update(store, draft_id, status="ready", task_json=json.dumps(asdict(spec), ensure_ascii=False,
                                                                    default=str), errors="")
        _cleanup(project, draft_id)
        return _row(store, draft_id) or {}
    _update(store, draft_id, status="failed", errors=errors[:2000])
    _cleanup(project, draft_id)
    return _row(store, draft_id) or {}


def _draft_copy(project: ProjectConfig, draft_id: int) -> str:
    """Отдельная копия (detached) для чтения моделью — корень проекта владельца не трогается."""
    base = workspace.worktree_path(project, 0).parent / f"draft-{draft_id}"
    if not base.exists():
        base.parent.mkdir(parents=True, exist_ok=True)
        workspace.git(project.root, "worktree", "add", "--detach", str(base), project.work_branch)
        from ahub.prepare import hide_secrets

        hide_secrets(project, str(base))
    (base / ".ahub").mkdir(exist_ok=True)
    return str(base)


def _cleanup(project: ProjectConfig, draft_id: int) -> None:
    base = workspace.worktree_path(project, 0).parent / f"draft-{draft_id}"
    if base.exists():
        workspace.git(project.root, "worktree", "remove", "--force", str(base), check=False)


def _read_draft(ws: str) -> dict:
    try:
        data = json.loads((Path(ws) / ".ahub" / "draft.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def preview(store: Store, draft_id: int, limit: int = 1500) -> str:
    row = _row(store, draft_id)
    if row is None:
        return f"нет черновика #{draft_id}"
    if row["status"] != "ready":
        return f"черновик #{draft_id}: {row['status']}" + (f" — {row['errors'][:500]}" if row["errors"] else "")
    d = json.loads(row["task_json"])
    kinds = {"scout": "разведка", "code": "код", "routine": "рутина", "review": "ревью"}
    lines = [f"Черновик #{draft_id} ({kinds.get(d['kind'], d['kind'])}): {d['title']}",
             f"Что сделать: {d['spec'][:600]}"]
    if d.get("paths"):
        lines.append("Можно менять: " + ", ".join(d["paths"]))
    if d.get("accept"):
        lines.append("Проверка: " + ", ".join(d["accept"]))
    if d.get("result_format"):
        lines.append(f"Результат: {d['result_format'][:200]}")
    lines.append("Ревью: " + ("нет" if not d.get("review_level") else f"уровень {d['review_level']}"))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def start(store: Store, project: ProjectConfig, draft_id: int) -> int:
    """Явный запуск: черновик → задача в очереди. Повтор — та же задача."""
    row = _row(store, draft_id)
    if row is None:
        raise ValueError(f"нет черновика #{draft_id}")
    if row["status"] == "started" and row["task_id"]:
        return int(row["task_id"])
    if row["status"] != "ready":
        raise ValueError(f"черновик #{draft_id} не готов: {row['status']}")
    d = json.loads(row["task_json"])
    spec = tasks.TaskSpec(**{**d, "kind": Kind(d["kind"])})
    t = tasks.create(store, spec, project, key=f"draft-{draft_id}")
    _update(store, draft_id, status="started", task_id=t.id)
    return t.id


def cancel(store: Store, draft_id: int) -> bool:
    row = _row(store, draft_id)
    if row is None or row["status"] == "started":
        return False
    _update(store, draft_id, status="cancelled")
    return True


def list_drafts(store: Store, limit: int = 10) -> list[dict]:
    with store.read() as c:
        return [dict(r) for r in c.execute("SELECT * FROM draft ORDER BY id DESC LIMIT ?", (limit,))]
