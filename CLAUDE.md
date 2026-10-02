# agent-hub

Code map — read it first, instead of learning the code from scratch:
@docs/ARCHITECTURE.md

Architecture — `docs/architecture.md`, contracts — `docs/contracts.md`.

After a notable structural change (a module, a state, a table, a process) — update `docs/ARCHITECTURE.md`.

Tests: `.venv/bin/python -m pytest -q` (no network, HOME is faked).

<!-- ahub:begin -->
## agent-hub
Tasks for worker models go through `ahub` (skill `ahub`). At session start — Monitor on `ahub watch`;
by event lines: `ahub status T<id>` → `ahub accept|rework|reject`. Summary — `ahub status`.
<!-- ahub:end -->