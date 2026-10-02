#!/usr/bin/env python3
"""Фейковый agy для тестов контракта: отдаёт сохранённые образцы NDJSON (tests/data/agy).

Каталог с образцами — в AHUB_AGY_FAKE_DATA. Тестовый скрипт, не часть пакета.
"""

from __future__ import annotations

import os
import pathlib
import sys

DATA = pathlib.Path(os.environ.get("AHUB_AGY_FAKE_DATA", "."))
REAL_ID = "f3abdf51-18b6-4b8d-b92e-33565bb400a3"


def sample(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def main(argv: list[str]) -> int:
    if "--version" in argv:
        print("1.2.15-fake")
        return 0
    if "models" in argv:
        sys.stdout.write(sample("models.txt"))
        return 0
    if "--json-schema" in argv:
        sys.stdout.write(sample("structured.ndjson"))
        return 0
    if "--conversation" in argv:
        asked = argv[argv.index("--conversation") + 1]
        sys.stdout.write(sample("resume.ndjson").replace(REAL_ID, asked))
        return 0
    sys.stdout.write(sample("hello.ndjson").replace('"OK', '"PONG'))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))