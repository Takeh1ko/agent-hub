"""Worker prompts: global/project/local guidance per role on top of a lean built-in layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ahub import paths
from ahub.config import PROJECT_FILE, ProjectConfig
from ahub.i18n import lang
from ahub.i18n import t as _t
from ahub.model import Kind
from ahub.store import Task

ROLES = ("all", "code", "routine", "scout", "review")
WARN_BYTES = 4 * 1024
REFUSE_BYTES = 16 * 1024
REPORT_LIMIT_KB = 12

DEFAULT_RULES = """# agent-hub worker rules
- You are in a separate repo copy (git worktree). Never go outside it.
- Do not touch real data, other databases, secrets (.env, keys, /etc); network only if the task explicitly requires it.
- The hub service directory is `.ahub/` (not in git): write your result and report there.
- Interfaces and names from the task are a contract: do not rename them.
"""


@dataclass(frozen=True)
class PromptLayer:
    scope: str
    role: str
    path: Path
    content: str
    exists: bool
    heading: str


def resolve_project_all_file(project: ProjectConfig) -> tuple[Path | None, bool]:
    """Returns (path, is_legacy). Checks .hub/prompts/all.md first, then legacy project.rules_path()."""
    hub_all = paths.project_prompts_dir(project.root) / "all.md"
    if hub_all.is_file():
        return hub_all, False
    rp = project.rules_path()
    if rp is not None and rp.is_file():
        return rp, True
    return None, False


def assemble_guidance(project: ProjectConfig, role: str) -> tuple[list[str], str, list[PromptLayer]]:
    """Assemble user guidance for a session role:
    Order:
      1. global all.md
      2. project all.md (or legacy rules)
      3. local all.md
      4. global <role>.md (if role != 'all')
      5. project <role>.md (if role != 'all')
      6. local <role>.md (if role != 'all')
    Returns:
      (sections, summary_line, layers)
    where each section has heading '## Global guidance', '## Project guidance', or '## Local guidance'.
    """
    candidates: list[tuple[str, str, Path, str]] = []
    # all.md of a, b, c
    candidates.append(("global", "all", paths.global_prompts_dir() / "all.md", "Global guidance"))
    p_all_path, _ = resolve_project_all_file(project)
    p_all_target = p_all_path if p_all_path is not None else paths.project_prompts_dir(project.root) / "all.md"
    candidates.append(("project", "all", p_all_target, "Project guidance"))
    candidates.append(("local", "all", paths.local_prompts_dir(project.name) / "all.md", "Local guidance"))

    # role.md of a, b, c
    if role != "all":
        candidates.append(("global", role, paths.global_prompts_dir() / f"{role}.md", "Global guidance"))
        candidates.append(("project", role, paths.project_prompts_dir(project.root) / f"{role}.md", "Project guidance"))
        candidates.append(("local", role, paths.local_prompts_dir(project.name) / f"{role}.md", "Local guidance"))

    sections: list[str] = []
    layers: list[PromptLayer] = []
    used_by_scope: dict[str, list[str]] = {"global": [], "project": [], "local": []}

    for scope, r, path, heading_name in candidates:
        content = ""
        exists = False
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8")
                exists = True
            except OSError:
                pass
        heading = f"## {heading_name}"
        layer = PromptLayer(scope=scope, role=r, path=path, content=content, exists=exists, heading=heading)
        layers.append(layer)
        if content.strip():
            sections.append(f"{heading}\n{content.strip()}")
            if r not in used_by_scope[scope]:
                used_by_scope[scope].append(r)

    # Form summary: e.g. "built-in + global(code) + project(all, code)"
    parts = ["built-in"]
    for sc in ("global", "project", "local"):
        roles_in_sc = used_by_scope[sc]
        if roles_in_sc:
            parts.append(f"{sc}({', '.join(roles_in_sc)})")
    summary = " + ".join(parts)

    return sections, summary, layers


def rules_text(project: ProjectConfig) -> str:
    """Legacy helper: read project all.md, fallback to project.rules_path() or DEFAULT_RULES."""
    p, _ = resolve_project_all_file(project)
    if p is not None and p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            pass
    return DEFAULT_RULES


def reply_language_line() -> str:
    """One line for prompts where the model writes human-readable text."""
    language = "Russian" if lang() == "ru" else "English"
    return f"Write the report and all human-readable fields in {language}."


def report_heading() -> str:
    """Essence heading in the hub language (views understands both)."""
    return "## Суть" if lang() == "ru" else "## Summary"


def arbiter_heading() -> str:
    """Arbiter decision heading in the hub language (review understands both)."""
    return "## Решение арбитра" if lang() == "ru" else "## Arbiter decision"


def orchestrator_heading(*, rework: bool = False) -> str:
    """Orchestrator notes heading in the hub language."""
    if lang() == "ru":
        return "## Указания оркестратора (доработка)" if rework else "## Указания оркестратора"
    return "## Orchestrator notes (rework)" if rework else "## Orchestrator notes"


def final_line() -> str:
    """Last-message requirement in the hub language."""
    if lang() == "ru":
        return "Last message — one line: «готово» or «заблокировано: причина»."
    return 'Last message — one line: "done" or "blocked: reason".'


def _header(task: Task) -> str:
    kind = {Kind.SCOUT: "scout", Kind.CODE: "code", Kind.REVIEW: "review", Kind.ROUTINE: "routine"}[task.kind]
    parts = [f"# Task {task.label} ({kind}): {task.title}"]
    if task.spec.strip():
        parts.append(task.spec.strip())
    read = task.limits.get("read") or []
    if read:
        parts.append("## Read first\n" + "\n".join(f"- `{p}`" for p in read))
    if task.result_format.strip():
        parts.append("## Expected result\n" + task.result_format.strip())
    return "\n\n".join(parts)


def scout_delivery() -> str:
    head = report_heading()
    return f"""## How to submit (required; overrides project rules about commits and reports)
1. Stay in the copy (git worktree); never touch real data or secrets. Change and commit nothing in the project — this is reconnaissance. Create files only in `.ahub/`.
2. Report — `.ahub/report.md` (<= {REPORT_LIMIT_KB} KB). First section — `{head}`: at most 10 lines, the key
   points needed for a decision. Cite `file:line` for every claim; say what you did not check.
3. Result — `.ahub/result.json`:
   {{"summary": "1-3 sentences", "status": "done", "questions": ["what is still unclear"], "notes": "what was not checked"}}
   If you cannot continue (no access, contradiction in the task) — "status": "blocked" and the reason in summary.
4. {reply_language_line()}
5. {final_line()}
"""


def scout_prompt(project: ProjectConfig, task: Task) -> str:
    sections, summary, _ = assemble_guidance(project, "scout")
    task.limits["prompts"] = summary
    return "\n\n".join([*sections, _header(task), scout_delivery()])


def repair_prompt(problem: str) -> str:
    return (f"Result is not in the required form: {problem}.\n"
            "Do not research anything new. Add only what is missing, strictly per the \"How to submit\" section "
            "(`.ahub/result.json`, for scout also `.ahub/report.md`).\n"
            f"{reply_language_line()}\n{final_line()}")


CONTINUE_PROMPT = ("The session was interrupted (silence or failure). Continue the task from where you stopped; "
                   "if everything is already done — submit the result per the \"How to submit\" section.")


STOP_PROMPT = ("The hub asks you to stop (budget or command). Do not start anything new: save what is done "
               "(for code — commit by name), write `.ahub/result.json` with the status of what is ready. "
               "Write the report and all human-readable fields in English. "
               'Last message — one line: "done".')


def stop_prompt() -> str:
    return ("The hub asks you to stop (budget or command). Do not start anything new: save what is done "
            "(for code — commit by name), write `.ahub/result.json` with the status of what is ready. "
            f"{reply_language_line()} {final_line()}")


def nudge_prompt(text: str) -> str:
    """Message of the orchestrator (`ahub nudge T12 "…"`) into the worker's own session."""
    return f"Message from the orchestrator:\n{(text or '').strip()}"


def code_delivery(task: Task) -> str:
    paths_list = ", ".join(f"`{p}`" for p in task.limits.get("paths") or [])
    accept = task.limits.get("accept") or []
    tests = ("\n".join(f"   - `{a}`" for a in accept)) if accept else "   (no acceptance — routine)"
    commit_lang = "Russian" if lang() == "ru" else "English"
    return f"""## Allowed files
{paths_list}
Need more — do not change, write it in the result notes.

## Acceptance (must be green)
{tests}

## Quality bar
- Smallest diff that does the task; match surrounding code (naming, comment density, idioms).
- No dead code, commented-out code, or duplicated helpers.
- No broad `except Exception` — catch what you expect.
- Every behaviour change gets a test that fails without it.
- Run the project's linter, if it has one, and the acceptance before the last commit.
- No new dependencies.

## How to submit (required)
1. Stay in the copy (git worktree); never touch real data or secrets. Commit as you go: `git add <paths>` by name (never `-A`/`.`), commit message in {commit_lang}. No uncommitted changes at the end.
2. Result — `.ahub/result.json`:
   {{"summary": "1-3 sentences", "status": "done", "commit": "<HEAD sha>", "files": ["changed files"],
    "tests": {{"cmd": "...", "ok": true, "tail": "last output lines"}}, "notes": "what is not done / open questions"}}
   If you cannot continue (contradiction in the task, no access) — "status": "blocked" and the reason in summary.
3. {reply_language_line()}
4. {final_line()}
"""


def code_prompt(project: ProjectConfig, task: Task) -> str:
    role = "routine" if task.kind is Kind.ROUTINE else "code"
    sections, summary, _ = assemble_guidance(project, role)
    task.limits["prompts"] = summary
    return "\n\n".join([*sections, _header(task), code_delivery(task)])


@dataclass(frozen=True)
class PromptCheckIssue:
    path: Path
    severity: str  # "error" | "warning" | "hint"
    message: str
    fix: str = ""


def check_prompts_for_project(project: ProjectConfig | None = None) -> list[PromptCheckIssue]:
    """Check prompts directories: unknown files, size over 4 KB, refusal size over 16 KB, legacy rules."""
    issues: list[PromptCheckIssue] = []
    dirs: list[tuple[str, Path]] = [("global", paths.global_prompts_dir())]
    if project is not None:
        dirs.append(("project", paths.project_prompts_dir(project.root)))
        dirs.append(("local", paths.local_prompts_dir(project.name)))

    for _scope, d in dirs:
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file():
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_unknown_file", path=str(entry), known=", ".join(f"{r}.md" for r in ROLES)),
                    fix="remove or rename",
                ))
                continue
            if entry.name not in {f"{r}.md" for r in ROLES}:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_unknown_file", path=str(entry), known=", ".join(f"{r}.md" for r in ROLES)),
                    fix="remove or rename",
                ))
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > REFUSE_BYTES:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_size", path=str(entry), kb=size / 1024),
                    fix="trim guidance under 16 KB",
                ))
            elif size > WARN_BYTES:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="warning",
                    message=_t("prompts.warn_size", path=str(entry), kb=size / 1024),
                    fix="trim guidance under 4 KB",
                ))

    if project is not None and project.rules:
        proj_all = paths.project_prompts_dir(project.root) / "all.md"
        cfg_file = Path(project.root) / PROJECT_FILE
        if proj_all.is_file():
            issues.append(PromptCheckIssue(
                path=cfg_file,
                severity="hint",
                message=_t("prompts.legacy_rules_ignored", rules=project.rules),
                fix="remove rules from .hub.toml",
            ))
        else:
            issues.append(PromptCheckIssue(
                path=cfg_file,
                severity="hint",
                message=_t("prompts.legacy_rules_hint", rules=project.rules),
                fix="move to .hub/prompts/all.md",
            ))

    return issues
