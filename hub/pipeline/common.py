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


def _section_text_of(card_text: str, name: str) -> str:
    """Текст раздела карточки через разбор H02; фолбэк — весь текст."""
    try:
        from hub.gate import lint as lint_mod

        lines = card_text.splitlines()
        return lint_mod._section_text(lines, name)
    except (ImportError, AttributeError):
        return card_text


def card_network_is_playerok(card_text: str) -> bool:
    """Задача с «Сеть: playerok» — не параллельно с такой же.

    Ищем только в разделе «Сеть»: упоминание в других разделах
    серийный режим не включает.
    """
    return "playerok" in _section_text_of(card_text, "Сеть").lower()


def parse_level(card_text: str) -> str:
    sec = _section_text_of(card_text, "Уровень")
    low = sec.lower()
    for lv in ("easy", "medium", "hard"):
        if lv in low:
            return lv
    return "medium"


def _expand_braces_str(s: str) -> list[str]:
    """Раскрыть первую {a,b}-группу в строке (рекурсивно)."""
    start = s.find("{")
    if start < 0:
        return [s]
    depth = 0
    end = -1
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end < 0:
        return [s]
    pre, body, post = s[:start], s[start + 1:end], s[end + 1:]
    parts = body.split(",")
    if len(parts) < 2:
        return [s]
    # Не трогаем JSON/прозу: альтернативы — только токены без пробелов и пунктуации.
    if any(not p or any(c in p for c in (" ", '"', "'", "`", ":", "{", "}"))
           for p in parts):
        return [s]
    out: list[str] = []
    for part in parts:
        for expanded in _expand_braces_str(pre + part + post):
            if expanded not in out:
                out.append(expanded)
    return out


def _expand_braces_token(token: str) -> list[str]:
    """Раскрыть {a,b} в одном токене (рекурсивно): `x{a,b}y` → [xay, xby]."""
    return _expand_braces_str(token)


def _expand_braces_in_backticks(section: str) -> str:
    """Раскрыть {a,b} внутри бэктиков до сплиттера H02.

    Сплиттер «Можно менять» (H02) дробит содержимое бэктика по [,;\\s]+,
    поэтому `hub/commands/{start,stop}.py` без раскрытия превращается
    в мусорный глоб `hub/commands/{start`. Раскрываем целую строку
    бэктика заранее (дробим только после раскрытия).
    """
    import re as _re

    def _sub(m) -> str:
        inner = m.group(1)
        toks: list[str] = []
        for variant in _expand_braces_str(inner):
            for tok in _re.split(r"[,;\s]+", variant.strip()):
                if tok:
                    toks.append(tok)
        return "`" + " ".join(toks) + "`"

    return _re.sub(r"`([^`]+)`", _sub, section)


def card_globs(card_text: str) -> list[str]:
    """Глобы «Можно менять» с раскрытыми brace-группами (общее для cycle/merge)."""
    try:
        from hub.gate import lint as lint_mod

        lines = card_text.splitlines()
        sec = lint_mod._section_text(lines, "Можно менять")
        sec = _expand_braces_in_backticks(sec)
        return [str(g) for g in lint_mod._can_change_globs(sec)]
    except (ImportError, AttributeError):
        return []


def clean_pycache(worktree: str) -> None:
    """Убрать __pycache__/.pytest_cache: pytest их создаёт, ворота видят грязь."""
    import shutil as _sh

    root = Path(worktree)
    try:
        for p in root.rglob("__pycache__"):
            try:
                if p.is_dir() and not p.is_symlink():
                    _sh.rmtree(p, ignore_errors=True)
            except OSError:
                continue
        for p in root.rglob("*.pyc"):
            try:
                if p.is_file() and not p.is_symlink():
                    p.unlink()
            except OSError:
                continue
        pc = root / ".pytest_cache"
        try:
            if pc.is_dir() and not pc.is_symlink():
                _sh.rmtree(pc, ignore_errors=True)
        except OSError:
            pass
    except OSError:
        pass


def meta_get(store, key: str) -> str | None:
    """Значение meta без долгого соединения."""
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return None
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        return str(row["value"] if isinstance(row, sqlite3.Row) else row[0])
    except sqlite3.Error:
        return None
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def meta_set(store, key: str, value: str) -> None:
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return
    try:
        con.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def meta_del(store, key: str) -> None:
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return
    try:
        con.execute("DELETE FROM meta WHERE key=?", (key,))
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def parse_reviewers_list(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return parts or []
