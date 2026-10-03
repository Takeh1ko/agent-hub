"""Task draft (architecture §5): a human writes the goal in their own words → model (drafter role) fills in the fields →
validation → preview → human edits/launches. Nothing starts without an explicit "Launch".

The model works in a separate project copy (read-only), the outcome is JSON with TaskSpec fields in `.ahub/draft.json`.
Validation is tasks.resolve; errors → one retry with their text, else the draft is "failed".
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ahub import log as hublog
from ahub import providers, registry, tasks, workspace
from ahub.config import ProjectConfig
from ahub.i18n import Words
from ahub.i18n import t as _t
from ahub.model import Kind, Role
from ahub.prompts import rules_text
from ahub.providers.base import RunSpec
from ahub.providers.opencode import extract_json
from ahub.providers.runner import run as run_session
from ahub.store import Store
from ahub.time import now_ms

_log = hublog.get("drafts")
KIND_WORDS: Words = Words("draft.kind_", ("scout", "code", "routine", "review"))
FIELDS = ("kind", "title", "spec", "result_format", "paths", "accept", "read", "review_level", "resources")

PROMPT = """You help the owner (not a programmer) file a task for agent-hub worker models. Read the project code
(change nothing) and build a task from their words.

## Owner words
{text}

## How to choose the type
- scout — investigate/find/explain, code does not change;
- code — change code (acceptance tests required: pytest nodes, new test files allowed);
- routine — tidy files/docs without logic (no tests);
- review — check already written code (needs input: branch/commit/files — put in spec).

## Project allowed files
{allowed}

## Answer
Write `.ahub/draft.json` and repeat it as your last message:
{{"kind": "scout|code|routine|review", "title": "goal in one phrase (<= 120 chars)",
  "spec": "details: what to do, where, how to verify, what not to touch",
  "result_format": "what result is needed (for scout — what the report must contain)",
  "paths": ["globs of allowed files (for code/routine)"], "accept": ["pytest nodes (for code)"],
  "read": ["what the worker should read first"], "review_level": 0}}
review_level: 0 — no review, 1 — plain docs/routine, 2 — plain code, 3-4 — code near money.
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
    raw_kind = str(data.get("kind", "scout")).strip().lower()
    kind_alias = {"разведка": "scout", "scout": "scout", "код": "code", "code": "code",
                  "рутина": "routine", "routine": "routine", "ревью": "review", "review": "review"}
    kind = Kind(kind_alias.get(raw_kind, raw_kind))
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
    from ahub.prompts import reply_language_line

    errors = ""
    for attempt in (1, 2):
        prompt = "\n\n".join([rules_text(project).strip(), PROMPT.format(
            text=row["text"], allowed=", ".join(project.allowed_paths) or "any",
            errors=f"\n## Previous attempt failed validation\n{errors}\nFix it." if errors else ""),
            reply_language_line()])
        r = run_session(providers.get(entry.provider), RunSpec(
            prompt=prompt, cwd=ws, model_id=entry.model_id, variant=entry.variant,
            log_path=str(Path(ws) / ".ahub" / f"draft_{attempt}.log"), timeout_s=15 * 60, idle_s=600))
        data = _read_draft(ws) or extract_json(r.final_text) or {}
        if not data:
            errors = _t("draft.no_json", outcome=r.outcome.value, err=r.error[:200])
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
    """Separate (detached) copy for model reading — the owner's project root untouched."""
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
        return _t("draft.no_draft", id=draft_id)
    if row["status"] != "ready":
        text = _t("draft.not_ready", id=draft_id, status=row["status"])
        if row["errors"]:
            text += _t("draft.not_ready_reason", err=row["errors"][:500])
        return text
    d = json.loads(row["task_json"])
    lines = [_t("draft.preview_head", id=draft_id, kind=KIND_WORDS.get(d["kind"], d["kind"]), title=d["title"]),
             _t("draft.preview_spec", text=d["spec"][:600])]
    if d.get("paths"):
        lines.append(_t("draft.preview_paths", items=", ".join(d["paths"])))
    if d.get("accept"):
        lines.append(_t("draft.preview_accept", items=", ".join(d["accept"])))
    if d.get("result_format"):
        lines.append(_t("draft.preview_format", text=d["result_format"][:200]))
    if not d.get("review_level"):
        lines.append(_t("draft.preview_no_review"))
    else:
        lines.append(_t("draft.preview_review", level=d["review_level"]))
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def hint(store: Store, draft_id: int) -> str:
    """What to run with this draft: start it — only a ready one can be started."""
    row = _row(store, draft_id)
    return _t("draft.next_start", id=draft_id) if row is not None and row["status"] == "ready" else ""


def start(store: Store, project: ProjectConfig, draft_id: int) -> int:
    """Explicit launch: draft → queued task. Repeat — same task."""
    row = _row(store, draft_id)
    if row is None:
        raise ValueError(_t("draft.no_draft", id=draft_id))
    if row["status"] == "started" and row["task_id"]:
        return int(row["task_id"])
    if row["status"] != "ready":
        raise ValueError(_t("draft.start_not_ready", id=draft_id, status=row["status"]))
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
