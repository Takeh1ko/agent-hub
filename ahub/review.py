"""Review panel (V14, contracts §3; port of hub/gate/verdict.py v1).

The reviewer is a fresh session: sees the task, diff, and gate summary, but not the orchestrator's reference
decisions; writes the verdict to `.ahub/review_r<N>_<model>.json`. Panel: all approve → Done; any changes →
rework (if rounds remain), else "Needs decision". dispute counts only with file+line+issue ≥ 50 chars per finding.
low-only findings never block. Duplicates (file, line, issue) collapse.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ahub import reasons, workspace
from ahub.config import ProjectConfig
from ahub.gates import GateResult
from ahub.model import Kind
from ahub.prompts import PromptLayer, assemble_guidance, orchestrator_heading, reply_language_line
from ahub.store import Task

VERDICTS = ("approve", "changes", "dispute")
_ARBITER = re.compile(r"^#{1,6}\s*(?:Решени[ея]\s+(арбитра|оркестратора)|(?:Arbiter|Orchestrator)\s+decision)"
                        r".*?(?=^#{1,6}\s|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE)


@dataclass
class Finding:
    severity: str
    file: str
    line: int | None
    issue: str
    fix: str = ""
    by: str = ""

    def key(self) -> tuple:
        norm = re.sub(r"\W+", "", self.issue.lower())[:80]
        return (self.file, self.line, norm)


@dataclass
class Review:
    model: str
    verdict: str  # as the reviewer wrote it
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""

    @property
    def effective(self) -> str:
        v = self.verdict
        if v == "dispute":
            ok = self.findings and all(f.file and f.line and len(f.issue) >= 50 for f in self.findings)
            v = "dispute" if ok else "changes"
        if v == "changes" and self.findings and all(f.severity == "low" for f in self.findings):
            return "approve"
        return v


def review_path(worktree: str, round_no: int, model: str) -> Path:
    return Path(worktree) / workspace.AHUB_DIR / f"review_r{round_no}_{model}.json"


def strip_arbiter(text: str) -> str:
    """Orchestrator reference decisions are never shown to the reviewer."""
    return _ARBITER.sub("", text)


def verdict_format() -> str:
    """Verdict shape from contracts §3 — one for both brief and retry."""
    return ('{"verdict": "approve|changes|dispute", "summary": "one sentence", "findings": [{"severity": '
            '"high|medium|low", "file": "path", "line": 12, "issue": "point, <= 300 chars", '
            '"fix": "what to do"}]}')


def verdict_repair_prompt(round_no: int, model: str) -> str:
    """One retry for a missing verdict — same session, JSON only."""
    out = review_path(".", round_no, model).as_posix().removeprefix("./")
    return (f"You did not write the verdict file `{out}` (or it is malformed). "
            f"Write it now in exactly this JSON format:\n{verdict_format()}\n"
            "Do not change any other files.")


def review_prompt(project: ProjectConfig, task: Task, diff: str, gate: GateResult, round_no: int,
                  model: str, *, notes: str = "") -> tuple[str, str, list[PromptLayer]]:
    """The prompt of one reviewer.

    A review task has no allowed files, no acceptance and no gates — its input is what is under review, so
    those sections are replaced by the input itself (`gate` is then an empty result).
    """
    out = review_path(".", round_no, model).as_posix().removeprefix("./")
    guidance_sections, summary, layers = assemble_guidance(project, "review")
    sections = [*guidance_sections,
                f"# Review of {task.label}: {task.title}\nYou are a reviewer in a fresh session; you have not seen "
                "the worker's work. Stay in the copy (git worktree); never touch real data, other databases, "
                "secrets (.env, keys, /etc); network only if the task explicitly requires it. "
                "Do not change or commit project files.",
                "## Task\n" + strip_arbiter(task.spec.strip() or "(empty description)")]
    if task.kind is Kind.REVIEW:
        sections.append(f"## Under review\n`{task.limits.get('input') or ''}`")
        sections.append("## Material under review\n```\n" + diff + "\n```")
        if notes:
            sections.append(f"{orchestrator_heading(rework=True)}\n{notes.strip()}")
    else:
        sections.append(f"## Allowed files\n{', '.join(task.limits.get('paths') or [])}\n"
                        f"## Acceptance\n{', '.join(task.limits.get('accept') or []) or '—'}")
        sections.append(f"## Gates (no models)\n{gate.summary()}" + (f"\nTest tail:\n```\n{gate.tests_tail}\n```"
                                                                      if gate.tests_tail else ""))
        sections.append("## Diff\n```diff\n" + diff + "\n```")
    if task.kind is Kind.REVIEW:
        check = ("## What to check\nMatch to the task; correctness of what the input shows; "
                 "security; races; resource leaks; blocking calls in async; edge cases. Style/taste — low only.")
    else:
        check = ("## What to check\nMatch to the task and acceptance; stub tests (pass on broken logic — "
                 "check by breaking the logic locally and reverting via git checkout); "
                 "tests that do not test (pass whatever the code does, mock the thing under test); "
                 "races; resource leaks; blocking calls in async; changes outside allowed files; "
                 "dead code, commented-out code, duplicated helpers; broad `except Exception`. Style/taste — low only.")
    sections += [
        check,
        f"## How to submit\nWrite `{out}`:\n{verdict_format()}\n"
        f"Each finding needs file, line, and a concrete fix. {reply_language_line()} "
        'Last message — one line: "done".',
    ]
    return "\n\n".join(sections), summary, layers


def parse(path: Path, model: str) -> Review | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    fs = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        line = f.get("line")
        try:
            line = int(line) if line is not None and str(line).strip() else None
        except (TypeError, ValueError):
            line = None
        sev = str(f.get("severity", "medium")).lower()
        fs.append(Finding(sev if sev in ("high", "medium", "low") else "medium", str(f.get("file", "")), line,
                          str(f.get("issue", ""))[:300], str(f.get("fix", ""))[:300], model))
    return Review(model, str(data["verdict"]), fs, str(data.get("summary", ""))[:300])


def dedup(findings: list[Finding]) -> list[Finding]:
    seen: dict[tuple, Finding] = {}
    order = {"high": 0, "medium": 1, "low": 2}
    for f in findings:
        k = f.key()
        if k not in seen:
            seen[k] = f
        elif f.by not in seen[k].by:
            seen[k].by += f", {f.by}"
    return sorted(seen.values(), key=lambda f: (order.get(f.severity, 1), f.file, f.line or 0))


def panel(reviews: list[Review], expected: list[str], round_no: int, max_rounds: int,
          *, rework: bool = True) -> tuple[str, str]:
    """(decision, reason): done | fix | decision. The reason is a stored code blob, not text.

    `rework` False — a review task: the verdicts are the result, not a gate before a fix, so every verdict
    in hand finishes it (only a missing one needs a decision).
    """
    got = {r.model for r in reviews}
    missing = [m for m in expected if m not in got]
    if missing:
        return "decision", reasons.dump("review_missing", items=", ".join(missing))
    if all(r.effective == "approve" for r in reviews):
        return "done", reasons.dump("review_agree")
    if not rework:  # a review task: the verdicts are the result — every finding of them counts
        return "done", reasons.dump("review_findings", n=len(dedup([f for r in reviews for f in r.findings])))
    if any(r.effective == "dispute" for r in reviews) and not any(r.effective == "changes" for r in reviews):
        return "decision", reasons.dump("review_dispute")
    blocking = blocking_findings(reviews)
    if round_no < max_rounds:
        return "fix", reasons.dump("review_findings", n=len(blocking))
    highs = sum(1 for f in blocking if f.severity == "high")
    return "decision", reasons.dump("review_exhausted", n=len(blocking), highs=highs)


def blocking_findings(reviews: list[Review]) -> list[Finding]:
    """Findings that hold a code task: not from an approving reviewer, not low. Deduplicated."""
    return dedup([f for r in reviews if r.effective != "approve" for f in r.findings if f.severity != "low"])


def fix_prompt(findings: list[Finding], gate: GateResult | None = None, notes: str = "") -> str:
    from ahub.prompts import final_line

    lines = ["Rework on review findings (same task, same session). Fix, commit by name, update "
             f"`.ahub/result.json` (commit = new HEAD). {reply_language_line()} {final_line()} "
             "Disputed — explain in notes."]
    if notes:
        lines.append(f"{orchestrator_heading()}\n" + notes.strip())
    for i, f in enumerate(findings, 1):
        where = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"{i}. [{f.severity}] {where} — {f.issue}" + (f"\n   fix: {f.fix}" if f.fix else ""))
    if gate is not None and gate.tests_ok is False:
        lines.append(f"## Failing acceptance\n`{gate.tests_cmd}`\n```\n{gate.tests_tail}\n```")
    return "\n\n".join(lines)
