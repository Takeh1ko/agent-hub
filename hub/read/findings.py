"""Сжатые замечания ревью: чтение review_*.json, дедуп, печать."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

ISSUE_MAX = 300

_RE_ALL = re.compile(r"^review_r\d+(?:_.*)?\.json$")


def _norm_issue(text: str) -> str:
    """Нижний регистр, схлопнутые пробелы."""
    return " ".join(str(text).lower().split())


@dataclass
class Finding:
    file: str
    line: int
    issue: str
    severity: str
    author: str


def _parse_one(data: dict, author: str) -> Finding | None:
    if not isinstance(data, dict):
        return None
    fname = data.get("file") or data.get("path") or data.get("filepath") or "?"
    try:
        lineno = int(data.get("line") or 0)
    except (TypeError, ValueError):
        lineno = 0
    issue = str(data.get("issue") or data.get("text") or data.get("message") or "")
    severity = str(data.get("severity") or data.get("level") or "info")
    # Живые сводные файлы пишут имя в поле reviewer (run_task.py), author — фолбэк.
    who = str(data.get("author") or data.get("reviewer") or author or "")
    return Finding(file=str(fname), line=lineno, issue=issue,
                   severity=severity, author=who)


def _author_of(path: Path) -> str:
    """Суффикс review_rN_<автор>.json, у сводного — пусто."""
    stem = path.stem  # review_r1_muse
    m = re.match(r"^review_r\d+(?:_(.*))?$", stem)
    if not m or not m.group(1):
        return ""
    return m.group(1)


def load_findings(worktree: Path, round: int | None = None) -> list[Finding]:
    """Читать <worktree>/.agent/review_r*.json, битые пропускать."""
    agent = Path(worktree) / ".agent"
    try:
        if not agent.is_dir():
            return []
    except OSError:
        return []
    if round is None:
        cands = sorted(agent.glob("review_r*.json"))
        pats = _RE_ALL
    else:
        cands = sorted(agent.glob(f"review_r{round}*.json"))
        pats = re.compile(rf"^review_r{round}(?:_.*)?\.json$")
    out: list[Finding] = []
    for path in cands:
        if not pats.match(path.name):
            continue
        try:
            if not path.is_file():
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        author = _author_of(path)
        if isinstance(raw, dict) and isinstance(raw.get("findings"), list):
            items = raw["findings"]
        elif isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict) and ("file" in raw or "issue" in raw):
            items = [raw]
        else:
            continue
        for entry in items:
            got = _parse_one(entry, author) if isinstance(entry, dict) else None
            if got is not None:
                out.append(got)
    return out


def dedup_findings(items: list[Finding]) -> list[Finding]:
    """Дедуп по (file, line, нормализованный issue), оставить первое."""
    seen: dict[tuple, Finding] = {}
    for f in items:
        key = (f.file, f.line, _norm_issue(f.issue))
        if key not in seen:
            seen[key] = f
    return sorted(seen.values(), key=lambda f: (f.file, f.line))


def format_findings(items: list[Finding], limit: int = 1500) -> str:
    """По одному на строку, issue до 300 симв., вывод до limit байт."""
    lines: list[str] = []
    for f in items:
        one = " ".join(f.issue.split())[:ISSUE_MAX]
        lines.append(f"{f.file}:{f.line} [{f.severity}] {one} ({f.author})")
    if not lines:
        return ""
    kept: list[str] = []
    for ln in lines:
        cand = "\n".join(kept + [ln])
        if len(cand.encode("utf-8")) > limit:
            break
        kept.append(ln)
    if not kept:
        # Первая строка длиннее лимита — режем по байтам.
        return lines[0].encode("utf-8")[:limit].decode("utf-8", "ignore")
    return "\n".join(kept)
