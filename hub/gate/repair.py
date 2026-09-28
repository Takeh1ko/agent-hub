"""Repair-промпт: мелкий провал чинится в той же сессии. Фиксированный текст."""

from __future__ import annotations

REPAIR_PROMPT: str = (
    "Почини мелкий провал в той же сессии:\n"
    "1. git status --short — посмотри незакоммиченное;\n"
    "2. закоммить поимённо: git add <пути> (никогда -A, не git add -A и не git add .);\n"
    "3. запиши .agent/done.json по схеме "
    '{"commit": sha, "files": [...], "tests": {"cmd": "..", "ok": true, "tail": ".."}, "notes": ".."};\n'
    "4. верни HEAD (git rev-parse HEAD)."
)


def repair_prompt(reason: str) -> str:
    """Фиксированный промпт + причина одной строкой."""
    return REPAIR_PROMPT + "\nПричина: " + reason
