# Правила работника agent-hub v2 (проект agent-hub)

- Ты в отдельной копии репозитория (git worktree) на своей ветке. Выходить за её пределы нельзя.
- Код — пакет `ahub/`, тесты — `tests/`. Карта кода — `docs/ARCHITECTURE.md`.
- Python: `/home/takehiko/Projects/Python/agent-hub/.venv/bin/python`; тесты:
  `/home/takehiko/Projects/Python/agent-hub/.venv/bin/python -m pytest -q <пути>` (сети нет, всё — во временных
  каталогах; `tests/conftest.py` подменяет HOME).
- Стиль: Python 3.12, `from __future__ import annotations`, аннотации, dataclasses, stdlib sqlite3; комментарии
  и тексты — по-русски, коротко; как окружающий код. Интерфейсы — по `docs/v2/contracts.md`.
- Настоящие данные не трогать: `~/.local/share/opencode`, `~/.local/share/ahub`, чужие процессы и репозитории.
- Зависимость, которой нет в `pyproject.toml`, не добавлять — напиши в notes.
