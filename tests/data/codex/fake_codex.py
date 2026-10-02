#!/usr/bin/env python3
"""Фейковый codex для тестов контракта: отдаёт сохранённые образцы событий (tests/data/codex).

Каталог с образцами — в AHUB_CODEX_FAKE_DATA. Тестовый скрипт, не часть пакета.
"""

from __future__ import annotations

import os
import pathlib
import sys

DATA = pathlib.Path(os.environ.get("AHUB_CODEX_FAKE_DATA", "."))
REAL_ID = "01a0fec9-9da7-7c71-9fa0-cdba1d4bfc35"


def sample(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print("codex-cli 0.153.4-fake")
        return 0
    if "login" in argv and "status" in argv:
        sys.stdout.write(sample("login_ok.txt"))
        return 0
    if "debug" in argv and "models" in argv:
        sys.stdout.write(sample("models.json"))
        return 0
    if "sandbox" in argv:
        return 0  # песочница на этой машине есть
    if "--output-schema" in argv:
        sys.stdout.write(sample("structured.ndjson"))
        return 0
    if "resume" in argv:
        asked = next((a for a in argv if a == REAL_ID), None)
        sys.stdout.write(sample("resume.ndjson").replace(REAL_ID, asked or REAL_ID))
        return 0
    if "-m" in argv and argv[argv.index("-m") + 1] == "broken-model":
        sys.stderr.write("Error: model metadata not found\n")
        sys.stdout.write(sample("model_error.ndjson"))
        return 1
    sys.stdout.write(sample("hello.ndjson").replace('"OK"', '"PONG"'))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
