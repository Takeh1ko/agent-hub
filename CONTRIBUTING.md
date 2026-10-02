# Contributing

Thanks for looking. Issues and pull requests are welcome — bug reports with a reproduction most of all.

## Setup

```
git clone https://github.com/Takeh1ko/agent-hub && cd agent-hub
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The suite takes about three minutes, needs no network and fakes `HOME`, so it never touches your real hub.
Tests against real providers cost money and run only on request: `AHUB_LIVE=1 .venv/bin/python -m pytest -m live`.

## Where things are

Start with [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — a one-page map of the modules and how a task flows
through them. The design is in [docs/architecture.md](docs/architecture.md), the interfaces between parts in
[docs/contracts.md](docs/contracts.md).

## Conventions

- Python 3.11+, `from __future__ import annotations`, dataclasses, stdlib `sqlite3`; time is passed in as a
  parameter so tests can control it.
- Comments and docstrings in English. Text a user or Claude sees goes through `ahub/i18n` (`t("key", ...)`),
  with the key in both `en.py` and `ru.py`; `tests/test_i18n.py` checks that the catalogs match and
  `tests/test_no_cyrillic.py` catches strings that bypass it.
- Event lines start with stable codes (`DONE`, `DECISION`, `ERROR`, `OWNER`, `ANSWER`, `ALARM`) — never
  translate or rename them, the Claude Code skill parses them.
- A new model provider is a new module in `ahub/providers/` implementing `providers/base.py`; it should pass the
  shared contract suite in `tests/provider_contract.py`. The core does not change.
- Keep changes small and covered by a test. If you touch structure (a module, a state, a table, a process),
  update `docs/ARCHITECTURE.md`.

## Platforms

Linux and macOS are tested in CI. Windows is supported through WSL2 only.
