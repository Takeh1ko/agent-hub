"""Панель ревью (V14, contracts §3; перенос hub/gate/verdict.py v1).

Ревьюер — новая сессия, видит задачу, дифф и итог ворот, но не решения-эталоны оркестратора; пишет вердикт в
`.ahub/review_r<N>_<model>.json`. Панель: все approve → Готово; есть changes → доработка (если круги остались),
иначе «Нужно решение». dispute засчитывается только с file+line+issue ≥ 50 символов у каждого замечания.
Замечания только low не держат задачу. Дубли (file, line, issue) схлопываются.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ahub import workspace
from ahub.config import ProjectConfig
from ahub.gates import GateResult
from ahub.prompts import rules_text
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
    verdict: str  # как записал ревьюер
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
    """Решения-эталоны оркестратора ревьюеру не показываются."""
    return _ARBITER.sub("", text)


def review_prompt(project: ProjectConfig, task: Task, diff: str, gate: GateResult, round_no: int,
                  model: str) -> str:
    out = review_path(".", round_no, model).as_posix().removeprefix("./")
    return "\n\n".join([
        rules_text(project).strip(),
        f"# Ревью задачи {task.label}: {task.title}\nТы ревьюер в новой сессии; работу исполнителя не видел. "
        "Файлы проекта не меняй и не коммить.",
        "## Постановка\n" + strip_arbiter(task.spec.strip() or "(описание пусто)"),
        f"## Разрешённые файлы\n{', '.join(task.limits.get('paths') or [])}\n"
        f"## Приёмка\n{', '.join(task.limits.get('accept') or []) or '—'}",
        f"## Ворота (без моделей)\n{gate.summary()}" + (f"\nХвост тестов:\n```\n{gate.tests_tail}\n```"
                                                        if gate.tests_tail else ""),
        "## Дифф\n```diff\n" + diff + "\n```",
        "## Что проверить\nСоответствие постановке и приёмке; тесты-пустышки (проходят при сломанной логике — "
        "проверь, сломав логику локально и откатив через git checkout); гонки; утечки ресурсов; блокирующие "
        "вызовы в async; выход за разрешённые файлы. Стиль/вкус — только low.",
        f"## Как сдать\nЗапиши `{out}`:\n"
        '{"verdict": "approve|changes|dispute", "summary": "одна фраза", "findings": [{"severity": '
        '"high|medium|low", "file": "путь", "line": 12, "issue": "суть ≤ 300 симв.", "fix": "что сделать"}]}\n'
        "Каждое замечание — с файлом, строкой и конкретным исправлением. Последнее сообщение — «готово».",
    ])


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


def panel(reviews: list[Review], expected: list[str], round_no: int, max_rounds: int) -> tuple[str, str]:
    """(решение, причина): done | fix | decision."""
    got = {r.model for r in reviews}
    missing = [m for m in expected if m not in got]
    if missing:
        return "decision", f"ревьюер(ы) не сдали вердикт: {', '.join(missing)}"
    if all(r.effective == "approve" for r in reviews):
        return "done", "ревью: все согласны"
    if any(r.effective == "dispute" for r in reviews) and not any(r.effective == "changes" for r in reviews):
        return "decision", "исполнитель/ревьюер спорят — нужно решение"
    blocking = dedup([f for r in reviews if r.effective != "approve" for f in r.findings if f.severity != "low"])
    if round_no < max_rounds:
        return "fix", f"ревью: {len(blocking)} замечаний"
    highs = sum(1 for f in blocking if f.severity == "high")
    return "decision", f"круги ревью кончились ({len(blocking)} замечаний, high: {highs})"


def fix_prompt(findings: list[Finding], gate: GateResult | None = None, notes: str = "") -> str:
    lines = ["Доработка по замечаниям (та же задача, та же сессия). Исправь, закоммить поимённо, обнови "
             "`.ahub/result.json` (commit = новый HEAD) и ответь «готово». Спорное — объясни в notes."]
    if notes:
        lines.append("## Указания оркестратора\n" + notes.strip())
    for i, f in enumerate(findings, 1):
        where = f"{f.file}:{f.line}" if f.line else f.file
        lines.append(f"{i}. [{f.severity}] {where} — {f.issue}" + (f"\n   исправить: {f.fix}" if f.fix else ""))
    if gate is not None and gate.tests_ok is False:
        lines.append(f"## Приёмка красная\n`{gate.tests_cmd}`\n```\n{gate.tests_tail}\n```")
    return "\n\n".join(lines)
