"""hub findings: сжатые замечания ревью по задаче.

Шапка — по какому коммиту было ревью (коммит исполнителя из
`.agent/done.json`, иначе HEAD worktree); замечание помечается
«возможно исправлено», если его файл:строка менялась в коммитах
ветки позже ревью (`git diff -U0 review..HEAD`, new-сторона ханка).
Без git-репозитория вывод как раньше (без шапки и пометок).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from hub.read.findings import ISSUE_MAX, dedup_findings, load_findings
from hub.store import Store

STALE_MARK = "· возможно исправлено"
_LIMIT = 1500


def _git_out(worktree: str, *args: str) -> str | None:
    """stdout git-команды или None (не репо/ошибка). Без сети, коротко."""
    try:
        r = subprocess.run(
            ["git", *args], cwd=worktree,
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _head_sha(worktree: str) -> str | None:
    out = _git_out(worktree, "rev-parse", "HEAD")
    sha = (out or "").strip().splitlines()
    s = sha[0].strip() if sha else ""
    return s or None


def _review_sha(worktree: str, head: str | None) -> str | None:
    """Коммит ревью: commit из .agent/done.json, иначе HEAD.

    done.json пишет исполнитель до ревью, ревью смотрит этот коммит;
    свежие круги дописывают коммиты поверх — пометки «возможно исправлено»
    считаются от этого коммита до HEAD.
    """
    try:
        import json as _json

        raw = (Path(worktree) / ".agent" / "done.json").read_text(encoding="utf-8")
        data = _json.loads(raw)
        cand = str((data or {}).get("commit") or "").strip() if isinstance(data, dict) else ""
    except (OSError, ValueError):
        cand = ""
    if cand:
        # Короткий sha тоже годится; проверяем, что объект есть в репо.
        if _git_out(worktree, "cat-file", "-e", cand) is not None:
            try:
                full = _git_out(worktree, "rev-parse", "--verify", cand)
                full_s = (full or "").strip().splitlines()
                return full_s[0].strip() if full_s and full_s[0].strip() else cand
            except (IndexError, AttributeError):
                return cand
        # done.json врёт (rebase/сброс) — честнее HEAD, чем чужой sha.
    return head


def _short(sha: str) -> str:
    return sha[:8] if len(sha) > 8 else sha


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _new_ranges(worktree: str, base: str, head: str, rel: str) -> list[tuple[int, int]] | None:
    """Диапазоны диффа base..head для файла (для пометки по строке).

    Обычно new-сторона ханка; чистое удаление (new count 0) new-стороны
    не имеет — помечаем и удалённые строки (old-диапазон), и соседние
    (±1 строка от new start: после удаления нумерация съезжает, замечание
    на строке рядом с точкой удаления тоже «возможно исправлено»).
    None — git не ответил (пометок нет, без ложных); [] — файл не менялся.
    """
    out = _git_out(worktree, "-c", "core.quotepath=false", "diff", "-U0", f"{base}..{head}", "--", rel)
    if out is None:
        return None
    ranges: list[tuple[int, int]] = []
    for line in out.splitlines():
        m = _HUNK_RE.match(line)
        if not m:
            continue
        try:
            old_start = int(m.group(1))
            old_count = int(m.group(2)) if m.group(2) is not None else 1
            new_start = int(m.group(3))
            new_count = int(m.group(4)) if m.group(4) is not None else 1
        except (TypeError, ValueError):
            continue
        if new_count <= 0:
            if old_count <= 0:
                continue
            ranges.append((old_start, old_start + old_count - 1))
            # Окрестность точки удаления в новых координатах: new_start
            # для чистого удаления — строка перед вырезанным куском
            # (например, удаление 3–4 даёт `@@ -3,2 +2,0 @@`), поэтому
            # ±1 от new_start накрывает и удалённую строку, и соседей.
            lo = max(1, new_start - 1)
            hi = new_start + 1
            if hi >= lo:
                ranges.append((lo, hi))
            continue
        ranges.append((new_start, new_start + new_count - 1))
    return ranges


def _stale_files(worktree: str, base: str, head: str) -> set[str] | None:
    # quotepath выключен, как в cycle._diff_files: иначе не-ASCII путь
    # приходит октальным квотингом (`"\321\204..."`) и не совпадает с rel.
    out = _git_out(worktree, "-c", "core.quotepath=false", "diff", "--no-renames",
                   "--name-only", f"{base}..{head}", "--")
    if out is None:
        return None
    return {l.strip() for l in out.splitlines() if l.strip()}


def _stale_of(items, worktree: str, base: str, head: str) -> set[int]:
    """Индексы замечаний, чья файл:строка менялась в base..HEAD.

    Консервативно: строка внутри new-ханка `git diff -U0` (чистое удаление —
    внутри old-диапазона того же ханка); line<=0 — по факту изменения
    файла; файл без пути («?») — не помечаем.
    """
    if not base or not head or base == head:
        return set()
    rels = sorted({str(f.file) for f in items if str(f.file) not in ("", "?")})
    if not rels:
        return set()
    changed = _stale_files(worktree, base, head)
    if changed is None:
        return set()
    ranges_cache: dict[str, list[tuple[int, int]] | None] = {}
    stale: set[int] = set()
    for i, f in enumerate(items):
        rel = str(f.file)
        if not rel or rel == "?" or rel not in changed:
            continue
        try:
            lineno = int(f.line or 0)
        except (TypeError, ValueError):
            lineno = 0
        if lineno <= 0:
            stale.add(i)
            continue
        if rel not in ranges_cache:
            ranges_cache[rel] = _new_ranges(worktree, base, head, rel)
        ranges = ranges_cache[rel]
        if ranges is None:
            continue
        if any(s <= lineno <= e for s, e in ranges):
            stale.add(i)
            continue
        # Файл удалён в HEAD — строка пропала вместе с ним.
        try:
            if not (Path(worktree) / rel).exists():
                stale.add(i)
        except OSError:
            pass
    return stale


def _format_with_stale(items, stale: set[int], limit: int = _LIMIT) -> str:
    """Как read.findings.format_findings, плюс пометка у stale."""
    lines: list[str] = []
    for i, f in enumerate(items):
        one = " ".join(str(f.issue).split())[:ISSUE_MAX]
        ln = f"{f.file}:{f.line} [{f.severity}] {one} ({f.author})"
        if i in stale:
            ln += f" {STALE_MARK}"
        lines.append(ln)
    if not lines:
        return ""
    kept: list[str] = []
    for ln in lines:
        cand = "\n".join(kept + [ln])
        if len(cand.encode("utf-8")) > limit:
            break
        kept.append(ln)
    if not kept:
        return lines[0].encode("utf-8")[:limit].decode("utf-8", "ignore")
    return "\n".join(kept)


def cmd_findings(args) -> int:
    store = Store()
    task = store.get_task(args.id)
    if task is None:
        print(f"нет задачи {args.id}", file=sys.stderr)
        return 1
    wt = str(task.get("worktree") or "")
    items = dedup_findings(load_findings(Path(wt), getattr(args, "round", None)) if wt else [])
    if not items:
        # Замечаний нет — пустой вывод, как до шапки (парсеры ждут пустоту).
        return 0
    # --fix: та же дедуп-печать для починки, файлы не правим.
    if not wt:
        from hub.read.findings import format_findings as _fmt

        text = _fmt(items)
        if text:
            print(text)
        return 0
    head = _head_sha(wt)
    if head is None:
        # Не git (фейковый worktree в тестах) — вывод как раньше.
        from hub.read.findings import format_findings as _fmt

        text = _fmt(items)
        if text:
            print(text)
        return 0
    review = _review_sha(wt, head) or head
    stale = _stale_of(items, wt, review, head)
    if review == head:
        header = f"ревью по коммиту {_short(review)}"
    else:
        header = f"ревью по коммиту {_short(review)} (HEAD {_short(head)})"
    budget = _LIMIT - len(header.encode("utf-8")) - 1
    body = _format_with_stale(items, stale, limit=max(budget, 0))
    if body:
        print(header + "\n" + body)
    else:
        print(header)
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("findings", help="замечания ревью (дедуп)")
    p.add_argument("id", help="ID задачи")
    p.add_argument("--round", type=int, default=None, help="круг ревью")
    p.add_argument("--fix", action="store_true",
                   help="печать для починки (то же, что без флага: только печать)")
    p.set_defaults(func=cmd_findings)
