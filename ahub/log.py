"""Логи хаба: JSON-строки в одном файле для всех компонентов и процессов, поиск WARNING/ERROR за период.

- Каждая запись: {"ts": мс, "lvl": "WARNING", "comp": "service", "msg": "...", "pid": 123, ...контекст}.
  Контекст (task, session, provider…) — через `get(component, **ctx)` или `extra={...}`.
- Пишут много процессов сразу: файл открыт на дозапись (строка — одна запись write), обработчик
  переоткрывает файл после ротации (WatchedFileHandler). Ротацию делает любой процесс под flock — один за раз.
- Наблюдатель читает `scan(since)` — записи уровня ≥ WARNING за период из текущего и ротированных файлов.
"""

from __future__ import annotations

import fcntl
import json
import logging
import logging.handlers
import os
import re
import sys
import traceback
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ahub import paths

LOG_NAME = "ahub.log"
MAX_BYTES = 5 * 1024 * 1024
KEEP = 5
_STD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


def log_file() -> Path:
    return paths.log_dir() / LOG_NAME


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": int(record.created * 1000),
            "lvl": record.levelname,
            "comp": getattr(record, "comp", None) or record.name.removeprefix("ahub.").removeprefix("ahub"),
            "msg": record.getMessage(),
            "pid": record.process,
        }
        for k, v in vars(record).items():
            if k not in _STD_ATTRS and k != "comp" and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = "".join(traceback.format_exception(*record.exc_info))[-4000:]
        return json.dumps(out, ensure_ascii=False, default=str)


class _Handler(logging.handlers.WatchedFileHandler):
    """Дозапись + переоткрытие после ротации; ротация по размеру под flock."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self.stream is not None and self.stream.tell() >= MAX_BYTES:
                rotate_if_needed()
        except (OSError, ValueError):
            pass
        super().emit(record)


class _Adapter(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


_configured: Path | None = None


def setup(level: str | int = "INFO", *, to_stderr: bool = False) -> None:
    """Подключить файл логов к логгеру «ahub». Повторный вызов с тем же файлом — без дублей."""
    global _configured
    root = logging.getLogger("ahub")
    target = log_file()
    if _configured != target:
        for h in list(root.handlers):
            if isinstance(h, _Handler):
                root.removeHandler(h)
                h.close()
        target.parent.mkdir(parents=True, exist_ok=True)
        h = _Handler(str(target), encoding="utf-8", delay=True)
        h.setFormatter(JsonFormatter())
        root.addHandler(h)
        _configured = target
    if to_stderr and not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)
    root.setLevel(level if isinstance(level, int) else _LEVELS.get(str(level).upper(), 20))
    root.propagate = False


def get(component: str, **ctx: Any) -> logging.LoggerAdapter:
    """Логгер компонента с постоянным контекстом: get("task", task=12).warning("…")."""
    if _configured != log_file():
        setup()
    return _Adapter(logging.getLogger(f"ahub.{component}"), {"comp": component, **ctx})


def install_excepthook(component: str) -> None:
    """Необработанное исключение процесса — в лог уровнем CRITICAL (и дальше как обычно)."""
    prev = sys.excepthook
    logger = get(component)

    def hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            logger.critical("необработанное исключение: %s", exc, exc_info=(exc_type, exc, tb))
        prev(exc_type, exc, tb)

    sys.excepthook = hook


def rotate_if_needed(max_bytes: int = MAX_BYTES, keep: int = KEEP) -> bool:
    """Ротация ahub.log → ahub.log.1 … .keep под flock. True — повернули."""
    f = log_file()
    lock_path = f.parent / ".rotate.lock"
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if not f.exists() or f.stat().st_size < max_bytes:
                return False
            for i in range(keep - 1, 0, -1):
                src = f.with_name(f"{LOG_NAME}.{i}")
                if src.exists():
                    os.replace(src, f.with_name(f"{LOG_NAME}.{i + 1}"))
            os.replace(f, f.with_name(f"{LOG_NAME}.1"))
            return True
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass
class ScanResult:
    records: list[dict]
    broken_lines: int  # строк, которые не разобрались как JSON (тоже сигнал наблюдателю)


def _files_since(since_ms: int) -> list[Path]:
    f = log_file()
    cands = [f.with_name(f"{LOG_NAME}.{i}") for i in range(KEEP, 0, -1)] + [f]
    out = []
    for p in cands:
        try:
            if p.exists() and p.stat().st_mtime * 1000 >= since_ms:
                out.append(p)
        except OSError:
            continue
    return out


def scan(since_ms: int, until_ms: int | None = None, *, min_level: str = "WARNING",
         component: str | None = None) -> ScanResult:
    """Записи уровня ≥ min_level за [since, until) — от старых к новым."""
    floor = _LEVELS.get(min_level.upper(), 30)
    records: list[dict] = []
    broken = 0
    for p in _files_since(since_ms):
        try:
            fh = p.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = int(rec["ts"])
                    lvl = _LEVELS.get(str(rec.get("lvl", "")).upper(), 0)
                except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                    broken += 1
                    continue
                if ts < since_ms or (until_ms is not None and ts >= until_ms) or lvl < floor:
                    continue
                if component is not None and rec.get("comp") != component:
                    continue
                records.append(rec)
    records.sort(key=lambda r: r["ts"])
    return ScanResult(records, broken)


_NUM = re.compile(r"\d+")


def signature(rec: dict) -> str:
    """Подпись записи без чисел: одна проблема с разными id — одна подпись (для паузы на повтор)."""
    return f"{rec.get('lvl')}|{rec.get('comp')}|{_NUM.sub('#', str(rec.get('msg', '')))[:160]}"


def summarize(records: list[dict], limit: int = 10) -> list[tuple[str, int]]:
    """Самые частые подписи: [(подпись, сколько)]."""
    return Counter(signature(r) for r in records).most_common(limit)
