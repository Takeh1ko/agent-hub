"""Чтение conversations agy — строго mode=ro, без записи в каталог agy."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class AgyConv:
    id: str
    path: str
    started_ms: int
    pulse_ms: int
    steps: int
    errors: int


def _pulse_ms(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError as e:
        log.warning("agy %s: нет mtime: %s", path, e)
        return None


def _one(db_path: Path, pulse_ms: int) -> AgyConv | None:
    """Одна conversations/*.db. Незнакомая схема → None + warning."""
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        log.warning("agy %s не открылась: %s", db_path, e)
        return None
    try:
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.Error as e:
            log.warning("agy %s: схема не читается: %s", db_path, e)
            return None
        if "steps" not in tables:
            log.warning("agy %s: незнакомая схема (нет steps)", db_path)
            return None
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info('steps')").fetchall()}
        except sqlite3.Error as e:
            log.warning("agy %s: схема не читается: %s", db_path, e)
            return None
        if "error_details" not in cols:
            log.warning("agy %s: незнакомая схема (нет error_details)", db_path)
            return None
        try:
            steps = int(con.execute("SELECT COUNT(*) FROM steps").fetchone()[0])
        except sqlite3.Error as e:
            log.warning("agy %s: незнакомая схема: %s", db_path, e)
            return None
        try:
            errors = int(con.execute(
                "SELECT COUNT(*) FROM steps WHERE error_details IS NOT NULL"
                " AND length(error_details) > 0").fetchone()[0])
        except sqlite3.Error as e:
            log.warning("agy %s: незнакомая схема: %s", db_path, e)
            return None
        # Старта в схеме нет, а st_ctime на Linux обновляется при каждой
        # записи — за старт его выдавать нельзя. Честно: старт = пульс (mtime).
        return AgyConv(id=db_path.stem, path=str(db_path),
                       started_ms=pulse_ms, pulse_ms=pulse_ms,
                       steps=steps, errors=errors)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def conversations(root: str | Path | None, since_ms: int) -> list[AgyConv]:
    """Разговоры agy. Незнакомая схема файла → пропуск + warning, не падение."""
    if root is None:
        return []
    if isinstance(root, str) and not root.strip():
        return []
    base = Path(root)
    try:
        if not base.is_dir():
            # Один файл тоже читаем (тесты/отладка), каталога нет — пусто.
            if base.is_file() and base.suffix == ".db":
                cands = [base]
            else:
                return []
        else:
            try:
                cands = sorted(base.glob("*.db"))
            except OSError as e:
                log.warning("agy %s: не читается каталог: %s", base, e)
                return []
    except OSError as e:
        log.warning("agy %s: не читается: %s", base, e)
        return []
    out: list[AgyConv] = []
    for db_path in cands:
        pulse = _pulse_ms(db_path)
        if pulse is None:
            continue
        if pulse < since_ms:
            continue
        try:
            conv = _one(db_path, pulse)
        except sqlite3.Error as e:
            log.warning("agy %s: незнакомая схема: %s", db_path, e)
            continue
        if conv is None:
            continue
        out.append(conv)
    out.sort(key=lambda c: c.pulse_ms)
    return out


def window_usage(root: str | Path | None, now_ms: int,
                 hours: int = 5) -> tuple[int, int]:
    """Квота окна: (запусков, шагов) за последние hours часов по пульсу."""
    try:
        window_ms = int(hours) * 3600_000
    except (TypeError, ValueError):
        window_ms = 5 * 3600_000
    convs = conversations(root, now_ms - window_ms)
    return (len(convs), sum(c.steps for c in convs))
