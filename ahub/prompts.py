"""Worker prompts: global/project/local guidance per role on top of a lean built-in layer.

Scope-major order: global (all, role) -> project (all, role) -> local (all, role).
The layers summary is a canonical English string built from structured per-scope
parts (``built-in + global(all, code) + ...``); readers render it in the hub
language with `render_summary`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ahub import log as hublog
from ahub import paths
from ahub.config import PROJECT_FILE, ProjectConfig
from ahub.i18n import lang
from ahub.i18n import t as _t
from ahub.model import Kind
from ahub.store import Task

_log = hublog.get("prompts")

ROLES = ("all", "code", "routine", "scout", "review")
WARN_BYTES = 4 * 1024
REFUSE_BYTES = 16 * 1024
REPORT_LIMIT_KB = 12

BOUNDARY = (
    "Stay in the copy (git worktree); never touch real data, other databases, secrets (.env, keys, /etc); "
    "network only if the task explicitly requires it. Interfaces and names from the task are a contract: "
    "do not rename them."
)

DEFAULT_RULES = f"""# agent-hub worker rules
- {BOUNDARY}
- The hub service directory is `.ahub/` (not in git): write your result and report there.
"""


@dataclass(frozen=True)
class PromptLayer:
    scope: str
    role: str
    path: Path
    content: str
    exists: bool
    heading: str
    skipped: str = ""  # why a present file was skipped: "" | "size" | "decode" | "unreadable"


@dataclass(frozen=True)
class ScopeUse:
    """Structured summary part: which roles of one scope made it into the prompt."""

    scope: str  # "global" | "project" | "local"
    roles: tuple[str, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()  # (role, reason): reason is "size" | "decode" | "unreadable"


# Canonical skip reason words (storage form; rendered via t() by render_summary).
SKIP_WORDS = {"size": "too big", "decode": "non-UTF-8", "unreadable": "unreadable"}

_SUMMARY_PART = re.compile(r"^(?P<scope>global|project|local)\((?P<items>.*)\)$")
_SUMMARY_ITEM = re.compile(r"^(?P<role>\w+)(?:: skipped, (?P<reason>too big|non-UTF-8|unreadable))?$")
_SUMMARY_TOKEN = re.compile(r"\w+(?:: skipped, (?:too big|non-UTF-8|unreadable))?")
_SKIP_WORD_KEYS = {"too big": "prompts.skipped_size", "non-UTF-8": "prompts.skipped_decode",
                   "unreadable": "prompts.skipped_unreadable"}


def resolve_project_all_file(project: ProjectConfig) -> Path | None:
    """Project all.md, falling back to the legacy rules= file; None when neither exists."""
    hub_all = paths.project_prompts_dir(project.root) / "all.md"
    if hub_all.is_file():
        return hub_all
    rp = project.rules_path()
    if rp is not None and rp.is_file():
        return rp
    return None


def assemble_guidance(project: ProjectConfig, role: str) -> tuple[list[str], str, list[PromptLayer]]:
    """Assemble user guidance for a session role:
    Order (scope-major):
      1. global all.md, then global <role>.md
      2. project all.md (or legacy rules), then project <role>.md
      3. local all.md, then local <role>.md
    Returns:
      (sections, summary_line, layers)
    where each non-empty scope has one heading: '## Global guidance', '## Project guidance', or '## Local guidance'.
    """
    p_all_path = resolve_project_all_file(project)
    p_all_target = p_all_path if p_all_path is not None else paths.project_prompts_dir(project.root) / "all.md"

    scopes = [
        ("global", "Global guidance", [
            ("all", paths.global_prompts_dir() / "all.md"),
            *( [(role, paths.global_prompts_dir() / f"{role}.md")] if role != "all" else [] ),
        ]),
        ("project", "Project guidance", [
            ("all", p_all_target),
            *( [(role, paths.project_prompts_dir(project.root) / f"{role}.md")] if role != "all" else [] ),
        ]),
        ("local", "Local guidance", [
            ("all", paths.local_prompts_dir(project.name) / "all.md"),
            *( [(role, paths.local_prompts_dir(project.name) / f"{role}.md")] if role != "all" else [] ),
        ]),
    ]

    sections: list[str] = []
    layers: list[PromptLayer] = []
    used: list[ScopeUse] = []

    for scope, heading_name, scope_candidates in scopes:
        heading = f"## {heading_name}"
        scope_texts: list[str] = []
        roles: list[str] = []
        skipped: list[tuple[str, str]] = []
        for r, path in scope_candidates:
            content = ""
            exists = False
            skip_reason = ""
            if path.is_file():
                try:
                    if path.stat().st_size > REFUSE_BYTES:
                        skip_reason = "size"
                    else:
                        content = path.read_text(encoding="utf-8")
                        exists = True
                except UnicodeDecodeError:
                    skip_reason = "decode"
                except OSError:
                    skip_reason = "unreadable"

                if skip_reason:
                    _log.warning("prompt guidance %s: skipped, %s", path, skip_reason)
                    skipped.append((r, skip_reason))
            layer = PromptLayer(scope=scope, role=r, path=path, content=content, exists=exists, heading=heading,
                                skipped=skip_reason)
            layers.append(layer)
            if content.strip():
                scope_texts.append(content.strip())
                if r not in roles:
                    roles.append(r)
        if scope_texts:
            sections.append(f"{heading}\n" + "\n\n".join(scope_texts))
        if roles or skipped:
            used.append(ScopeUse(scope=scope, roles=tuple(roles), skipped=tuple(skipped)))

    return sections, build_summary(used), layers


def build_summary(used: list[ScopeUse]) -> str:
    """Canonical (English) summary from structured parts, e.g. "built-in + project(all, code)"."""
    parts = ["built-in"]
    for u in used:
        items = list(u.roles) + [f"{r}: skipped, {SKIP_WORDS[reason]}" for r, reason in u.skipped]
        parts.append(f"{u.scope}({', '.join(items)})")
    return " + ".join(parts)


def render_summary(summary: str) -> str:
    """Render a canonical summary in the hub language, one word at a time (no stored translations)."""
    parts = summary.split(" + ")
    if not parts:
        return summary
    out = [_t("prompts.scope_builtin") if parts[0] == "built-in" else parts[0]]
    for part in parts[1:]:
        m = _SUMMARY_PART.match(part)
        if not m:
            out.append(part)
            continue
        scope_key = f"prompts.scope_{m.group('scope')}"
        out.append(f"{_t(scope_key)}({_render_items(m.group('items'))})")
    return " + ".join(out)


def _render_items(inner: str) -> str:
    """Render one scope's item list; unknown shapes pass through untouched."""
    tokens = _SUMMARY_TOKEN.findall(inner)
    if not tokens and inner:
        return inner
    if ", ".join(tokens) != inner:
        return inner
    rendered = []
    for tok in tokens:
        m = _SUMMARY_ITEM.match(tok)
        assert m is not None  # findall only yields grammar items
        if m.group("reason") is None:
            rendered.append(m.group("role"))
        else:
            rendered.append(f"{m.group('role')}: {_t('prompts.skipped')}, "
                            f"{_t(_SKIP_WORD_KEYS[m.group('reason')])}")
    return ", ".join(rendered)


def rules_text(project: ProjectConfig) -> str:
    """Legacy helper: read project all.md, fallback to project.rules_path() or DEFAULT_RULES."""
    p = resolve_project_all_file(project)
    if p is not None and p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
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
1. Change and commit nothing in the project — this is reconnaissance. Create files only in `.ahub/` (the hub service directory, not in git).
2. Report — `.ahub/report.md` (<= {REPORT_LIMIT_KB} KB). First section — `{head}`: at most 10 lines, the key
   points needed for a decision. Cite `file:line` for every claim; say what you did not check.
3. Result — `.ahub/result.json`:
   {{"summary": "1-3 sentences", "status": "done", "questions": ["what is still unclear"], "notes": "what was not checked"}}
   If you cannot continue (no access, contradiction in the task) — "status": "blocked" and the reason in summary.
4. {reply_language_line()}
5. {final_line()}
"""


def scout_prompt(project: ProjectConfig, task: Task) -> tuple[str, str, list[PromptLayer]]:
    sections, summary, layers = assemble_guidance(project, "scout")
    return "\n\n".join([*sections, _header(task), BOUNDARY, scout_delivery()]), summary, layers


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


def sync_conflict_prompt(project: ProjectConfig) -> str:
    """One resolve turn after the hub's sync merge hit a conflict (same worker session)."""
    branch = project.work_branch
    return (f"Main moved while you worked. Merge `{branch}` into your branch, resolve the conflicts "
            "keeping both sides, run the tests, commit by name, update `.ahub/result.json` "
            "(commit = new HEAD).")


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
1. Commit as you go: `git add <paths>` by name (never `-A`/`.`), commit message in {commit_lang}. No uncommitted changes at the end.
2. Result — `.ahub/result.json` (the hub service directory is `.ahub/`, not in git):
   {{"summary": "1-3 sentences", "status": "done", "commit": "<HEAD sha>", "files": ["changed files"],
    "tests": {{"cmd": "...", "ok": true, "tail": "last output lines"}}, "notes": "what is not done / open questions"}}
   If you cannot continue (contradiction in the task, no access) — "status": "blocked" and the reason in summary.
3. {reply_language_line()}
4. {final_line()}
"""


def code_prompt(project: ProjectConfig, task: Task) -> tuple[str, str, list[PromptLayer]]:
    role = "routine" if task.kind is Kind.ROUTINE else "code"
    sections, summary, layers = assemble_guidance(project, role)
    return "\n\n".join([*sections, _header(task), BOUNDARY, code_delivery(task)]), summary, layers


@dataclass(frozen=True)
class PromptCheckIssue:
    path: Path
    severity: str  # "error" | "warning" | "hint"
    message: str
    fix: str = ""


def check_prompts_for_project(project: ProjectConfig | None = None) -> list[PromptCheckIssue]:
    """Check prompts directories: unknown files, size over 4 KB, refusal size over 16 KB, legacy rules."""
    issues: list[PromptCheckIssue] = []
    dirs = [paths.global_prompts_dir()]
    if project is not None:
        dirs.append(paths.project_prompts_dir(project.root))
        dirs.append(paths.local_prompts_dir(project.name))

    for d in dirs:
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or entry.name not in {f"{r}.md" for r in ROLES}:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="warning",
                    message=_t("prompts.warn_unknown_file", path=str(entry), known=", ".join(f"{r}.md" for r in ROLES)),
                    fix=_t("prompts.fix_unknown_file"),
                ))
                continue
            try:
                entry.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_decode", path=str(entry)),
                    fix=_t("prompts.fix_decode"),
                ))
                continue
            except OSError as e:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_unreadable", path=str(entry), err=str(e)),
                    fix=_t("prompts.fix_unreadable"),
                ))
                continue
            try:
                size = entry.stat().st_size
            except OSError as e:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_unreadable", path=str(entry), err=str(e)),
                    fix=_t("prompts.fix_unreadable"),
                ))
                continue
            if size > REFUSE_BYTES:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="error",
                    message=_t("prompts.err_size", path=str(entry), kb=size / 1024),
                    fix=_t("prompts.fix_trim_16kb"),
                ))
            elif size > WARN_BYTES:
                issues.append(PromptCheckIssue(
                    path=entry,
                    severity="warning",
                    message=_t("prompts.warn_size", path=str(entry), kb=size / 1024),
                    fix=_t("prompts.fix_trim_4kb"),
                ))

    if project is not None and project.rules:
        proj_all = paths.project_prompts_dir(project.root) / "all.md"
        cfg_file = Path(project.root) / PROJECT_FILE
        if proj_all.is_file():
            issues.append(PromptCheckIssue(
                path=cfg_file,
                severity="hint",
                message=_t("prompts.legacy_rules_ignored", rules=project.rules),
                fix=_t("prompts.fix_legacy_ignored"),
            ))
        else:
            issues.append(PromptCheckIssue(
                path=cfg_file,
                severity="hint",
                message=_t("prompts.legacy_rules_hint", rules=project.rules),
                fix=_t("prompts.fix_legacy_rules"),
            ))

    return issues
