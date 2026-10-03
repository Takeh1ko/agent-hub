"""Worker prompts: minimal, per result contract (contracts §2). Project rules first."""

from __future__ import annotations

from ahub.config import ProjectConfig
from ahub.i18n import lang
from ahub.model import Kind
from ahub.store import Task

DEFAULT_RULES = """# agent-hub worker rules
- You are in a separate repo copy (git worktree). Never go outside it.
- Do not touch real data, other databases, secrets (.env, keys, /etc); network only if the task explicitly requires it.
- The hub service directory is `.ahub/` (not in git): write your result and report there.
- Interfaces and names from the task are a contract: do not rename them.
"""

REPORT_LIMIT_KB = 12


def rules_text(project: ProjectConfig) -> str:
    p = project.rules_path()
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


SCOUT_DELIVERY = f"""## How to submit (required; overrides project rules about commits and reports)
1. Change and commit nothing in the project — this is reconnaissance. Create files only in `.ahub/`.
2. Report — `.ahub/report.md` (<= {REPORT_LIMIT_KB} KB). First section — `## Summary`: at most 10 lines, the key
   points needed for a decision. Then details with `file:line` paths.
3. Result — `.ahub/result.json`:
   {{"summary": "1-3 sentences", "status": "done", "questions": ["what is still unclear"], "notes": "what was not checked"}}
   If you cannot continue (no access, contradiction in the task) — "status": "blocked" and the reason in summary.
4. Write the report and all human-readable fields in English.
5. Last message — one line: "done" or "blocked: reason".
"""


def scout_delivery() -> str:
    head = report_heading()
    return f"""## How to submit (required; overrides project rules about commits and reports)
1. Change and commit nothing in the project — this is reconnaissance. Create files only in `.ahub/`.
2. Report — `.ahub/report.md` (<= {REPORT_LIMIT_KB} KB). First section — `{head}`: at most 10 lines, the key
   points needed for a decision. Then details with `file:line` paths.
3. Result — `.ahub/result.json`:
   {{"summary": "1-3 sentences", "status": "done", "questions": ["what is still unclear"], "notes": "what was not checked"}}
   If you cannot continue (no access, contradiction in the task) — "status": "blocked" and the reason in summary.
4. {reply_language_line()}
5. {final_line()}
"""


def scout_prompt(project: ProjectConfig, task: Task) -> str:
    return "\n\n".join([rules_text(project).strip(), _header(task), scout_delivery()])


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
    paths = ", ".join(f"`{p}`" for p in task.limits.get("paths") or [])
    accept = task.limits.get("accept") or []
    tests = ("\n".join(f"   - `{a}`" for a in accept)) if accept else "   (no acceptance — routine)"
    commit_lang = "Russian" if lang() == "ru" else "English"
    return f"""## Allowed files
{paths}
Need more — do not change, write it in the result notes.

## Acceptance (must be green)
{tests}

## How to submit (required)
1. Commit as you go: `git add <paths>` by name (never `-A`/`.`), commit message in {commit_lang}. No uncommitted changes at the end.
2. Result — `.ahub/result.json`:
   {{"summary": "1-3 sentences", "status": "done", "commit": "<HEAD sha>", "files": ["changed files"],
    "tests": {{"cmd": "...", "ok": true, "tail": "last output lines"}}, "notes": "what is not done / open questions"}}
   If you cannot continue (contradiction in the task, no access) — "status": "blocked" and the reason in summary.
3. {reply_language_line()}
4. {final_line()}
"""


def code_prompt(project: ProjectConfig, task: Task) -> str:
    return "\n\n".join([rules_text(project).strip(), _header(task), code_delivery(task)])
