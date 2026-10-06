# Changelog

## 3.2.0 — 2026-10-06

### Added

- Model catalog: `ahub models` shows every model with its provider, real name and vendor, reasoning levels, plan
  (free / Go plan / pay-as-you-go / subscription), price per 1M tokens, context and the roles it serves; the
  provider headers show the Go plan's month spend and the Gemini quota. The same data in `setup`, `providers` and
  the console's `/models`.
- Reasoning effort as its own choice: `ahub task new --effort`, `ALIAS:EFFORT` in role defaults and model switches
  (`/model T12 spark:high`), validated against the model's levels. Legacy aliases (`spark-high`, `spark-medium`,
  `gemini-low`) keep working and are hidden from menus.
- Busy-loop guard: a task that settles for the same reason three times without progress, or a worker that gets
  three empty continues in a row, stops and asks instead of spinning; status shows holds as holds; the observer
  alarms on frequent re-picks.

### Fixed

- Quota holds no longer spin: a task waiting for a reviewer's quota resumes straight at the review (no worker turn,
  no gates re-run), is re-checked at the quota reset or with a back-off, tells the owner once, and falls back to the
  next reviewer in the menu that has quota.
- Console: typed text after a click into the input no longer acts on the selected task (Enter followed it,
  a/x/r/m accepted/rejected/reworked/nudged it).
- Tests no longer start real providers or wait on fixed pauses: console tests wait for state, the quota tests used a
  real `opencode` turn over the network, and a test that launches a real provider binary now fails at once.

## 3.1.0 — 2026-10-05

### Added

- Codex provider: OpenAI Codex CLI with a configurable OS sandbox mode.
- `ahub providers`: enable and disable providers; `ahub setup` probes models live and picks working per-role defaults.
- Per-provider proxy (`[providers.<name>]` proxy/no_proxy), used by workers and shown by `ahub doctor`.
- `ahub follow`: live readable transcript of a worker session.
- `ahub nudge`: message a working agent in its session without restarting it.
- `ahub top` shows current tasks by default, with a live transcript screen.
- Per-project scope: `--all` / `--project` on commands, presence and Telegram launcher per project.
- `ahub projects` (hub-wide overview) and `ahub cost` (per project and per model).
- `task new --kind review`: review a branch, a commit, a range or files.
- `ahub inbox <id>` / `questions <id>`: read a message in full.
- Interactive console: bare `ahub` opens the terminal UI (tasks, alerts, transcript, input).
- Prompt system: global/project/local guidance per role (`ahub prompts`) over a lean built-in layer.
- Quota-aware scheduling: reads `agy /usage`, holds or falls back below thresholds, requeues on quota errors.

### Changed

- New CLI output in the style of Claude Code on a TTY (pipe output unchanged), with grouped `--help`.
- Task branches are kept in sync with the work branch before checks — fewer merge-conflict rounds.
- One acceptance at a time per project, verified in a temp worktree before the branch moves.
- `ahub budget` can lower a task budget; the Telegram bot restarts onto new code like the service.
- Acceptance takes per-project pytest args and the suite runs in parallel; CI runs a smoke test as a new user.
- New short user guide (`docs/guide.md` / `docs/guide.ru.md`); architecture and contracts caught up.

### Fixed

- Audit findings across CLI output, setup/doctor/providers, project scope, events, Telegram and the console.
- The store retries on `database is locked` instead of failing the task; a broken `result.json` is reported, not fatal.
- Tests can no longer write into the real hub database (leak fixed and guarded).

## 3.0.0 — 2026-10-03

First public release.

- Package `ahub` on PyPI; command `ahub`; Python 3.11+.
- `ahub setup` wizard (language, project, providers, models, OS service, Claude Code skill, Telegram) and
  `ahub doctor` (what is wrong and how to fix it).
- English and Russian interface; stable event codes for Claude (`DONE`, `DECISION`, `ERROR`, `OWNER`, `ANSWER`,
  `ALARM`); worker prompts in English, answers in the hub language.
- Providers: opencode and Google Antigravity (`agy`, Gemini).
- macOS support (psutil, launchd); `ahub service start|stop` without an OS service; Windows via WSL2.
- Telegram is optional (`ahub[telegram]`); settings live in the hub config.
- Reviewer that does not hand in a verdict gets one retry in the same session; the observer only judges log
  entries inside its check window.

Versions 1 and 2 were private.
