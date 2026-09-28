"""Общее для команд конвейера: хеши, база, доп. поля, очередь."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path


def card_hash_of(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def current_base(project, repo: str | None = None) -> str:
    """base_sha старта: work_branch проекта, иначе текущий HEAD."""
    cwd = repo or str(getattr(project, "root", "") or ".")
    ref = (getattr(project, "work_branch", "") or "").strip()
    cands = [ref] if ref else []
    cands += ["HEAD"]
    for r in cands:
        if not r:
            continue
        try:
            out = subprocess.run(["git", "rev-parse", r], cwd=cwd,
                                 capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    return ""


def merge_base(repo: str, a: str, b: str) -> str:
    try:
        r = subprocess.run(["git", "merge-base", a, b], cwd=repo,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def read_extra(task: dict) -> tuple[int, str, bool]:
    """rounds/after_id/blind из задачи (колонки 004; нет — дефолты)."""
    try:
        rounds = int(task.get("rounds") or 2)
    except (TypeError, ValueError):
        rounds = 2
    if rounds < 1:
        rounds = 1
    after = str(task.get("after_id") or "")
    try:
        blind = bool(int(task.get("blind") or 0))
    except (TypeError, ValueError):
        blind = False
    return rounds, after, blind


def write_extra(store, task_id: str, rounds: int, after_id: str, blind: bool) -> None:
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return
    try:
        con.execute("UPDATE task SET rounds=?, after_id=?, blind=? WHERE id=?",
                    (int(rounds), after_id or "", 1 if blind else 0, task_id))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def is_paused(store) -> bool:
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return False
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT value FROM meta WHERE key='queue_paused'").fetchone()
        if row is None:
            return False
        val = row["value"] if isinstance(row, sqlite3.Row) else row[0]
        return str(val).strip() in ("1", "true", "yes", "on", "paused")
    except sqlite3.Error:
        return False
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def card_text_of(task: dict, project) -> str | None:
    rel = str(task.get("card_path") or "")
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute() and p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return None
    root = str(getattr(project, "root", "") or "")
    cands: list[Path] = []
    if root:
        cands.append(Path(root) / rel)
    wt = str(task.get("worktree") or "")
    if wt:
        cands.append(Path(wt) / rel)
    cands.append(Path(rel))
    for c in cands:
        try:
            if c.is_file():
                return c.read_text(encoding="utf-8")
        except OSError:
            continue
    return None


def card_network_is_playerok(card_text: str) -> bool:
    """Задача с «Сеть: playerok» — не параллельно с такой же."""
    low = card_text.lower()
    return "playerok" in low


def parse_level(card_text: str) -> str:
    for line in card_text.splitlines():
        s = line.strip()
        if "Уровень" in s:
            low = s.lower()
            for lv in ("easy", "medium", "hard"):
                if lv in low:
                    return lv
    return "medium"


def parse_reviewers_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return parts or []
