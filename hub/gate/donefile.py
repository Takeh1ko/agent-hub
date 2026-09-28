"""done.json исполнителя: чтение и проверка схемы. Чистые функции."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DoneFile:
    commit: str
    files: list[str]
    cmd: str
    ok: bool
    tail: str
    notes: str


def load_done(worktree: Path) -> DoneFile:
    """Прочитать <worktree>/.agent/done.json и проверить схему.

    Схема: {"commit": sha, "files": [..], "tests": {"cmd": "..",
    "ok": true, "tail": ".."}, "notes": ".."}.
    Нет файла → FileNotFoundError, битый JSON/схема → ValueError,
    текст всегда начинается с "done.json: " и называет поле.
    """
    path = Path(worktree) / ".agent" / "done.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"done.json: нет файла {path}") from None
    except OSError as e:
        raise FileNotFoundError(f"done.json: нет файла {path}: {e}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"done.json: битый JSON: {e}") from None
    if not isinstance(data, dict):
        raise ValueError("done.json: корень должен быть объектом {...}")
    commit = data.get("commit")
    if not isinstance(commit, str) or not commit.strip():
        raise ValueError("done.json: поле commit должно быть строкой sha")
    files = data.get("files")
    if not isinstance(files, list) or any(not isinstance(f, str) for f in files):
        raise ValueError("done.json: поле files должно быть списком строк [..]")
    tests = data.get("tests")
    if not isinstance(tests, dict):
        raise ValueError('done.json: поле tests должно быть объектом {"cmd", "ok", "tail"}')
    cmd = tests.get("cmd")
    if not isinstance(cmd, str):
        raise ValueError("done.json: поле tests.cmd должно быть строкой")
    ok = tests.get("ok")
    if type(ok) is not bool:
        raise ValueError("done.json: поле tests.ok должно быть true/false")
    tail = tests.get("tail")
    if not isinstance(tail, str):
        raise ValueError("done.json: поле tests.tail должно быть строкой")
    notes = data.get("notes")
    if not isinstance(notes, str):
        raise ValueError("done.json: поле notes должно быть строкой")
    return DoneFile(commit=commit, files=list(files), cmd=cmd, ok=ok, tail=tail, notes=notes)
