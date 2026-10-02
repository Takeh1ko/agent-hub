# agent-hub worker rules (the agent-hub project)

- You are in a separate copy of the repository (a git worktree) on your own branch. You must not go outside it.
- The code is the `ahub/` package, the tests are `tests/`. The code map is `docs/ARCHITECTURE.md`.
- Python: `.venv/bin/python`; tests: `.venv/bin/python -m pytest -q <paths>` (no network, everything in temp
  directories; `tests/conftest.py` fakes HOME). The whole suite takes about 2.5 minutes. If this copy has no
  `.venv/`, take the interpreter from the project config (`python` in `.hub.toml`).
- Style: Python 3.12, `from __future__ import annotations`, annotations, dataclasses, stdlib sqlite3; comments and
  docstrings in English, short, like the surrounding code. User-facing text goes through `ahub/i18n`
  (`t(key, **kw)`; the keys live in `ahub/i18n/en.py` and `ahub/i18n/ru.py` in the same order) — not in literals.
  Interfaces follow `docs/contracts.md`.
- Commit as you go: `git add <paths>` by name (never `-A`/`.`), commit message in Russian. No uncommitted changes at
  the end.
- Do not touch real data: `~/.local/share/opencode`, `~/.local/share/ahub`, other people's processes and repositories.
- Change only the files listed as allowed. If you need more — do not change it, write it in the result notes.
- A dependency that is not in `pyproject.toml` — do not add it; write it in the notes.