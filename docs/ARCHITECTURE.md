# agent-hub v2 — code map (a cheat sheet for Claude)

A condensed map, so the project does not have to be learned from scratch. Architecture — `docs/architecture.md`,
interfaces between the parts — `docs/contracts.md`.

## What it is
A service through which an orchestrator (Claude Code; any CLI agent — through CLI or MCP) and a human (the `ahub top`
terminal, Telegram) hand work to cheap worker models (opencode: Spark 1.3 and others), watch it and accept the result.
The command is `ahub` (package ahub; the `hub` script was removed from the package — it conflicted with the GitHub CLI
`hub`).

## How a task runs
```
ahub task new (tasks.py: field checks, defaults by kind)  → task: queued
ahub service (service.py, systemd ahub.service): queue, slots, resources, "after X" → spawn `python -m ahub.worker T12`
worker.py → engine.py (task owner, lease):
  scout:  prepare(copy) → working → result in the shape (.ahub/result.json + report.md) → done
  code/routine: prepare.py (copy without secrets, hooks, acceptance collection) → working → checking (gates.py: commit,
             diff ⊆ paths, result.json, acceptance under the lock) → reviewing (review.py: panel in new sessions) →
             fixing → … → done
  turn results (providers/runner.py → Outcome): network failure → retry; silence → one continuation;
  quota/timeout/budget → needs_decision; error → error; stop → stopped
events.py: codes DONE/DECISION/ERROR/OWNER/ANSWER/ALARM (ALARM! — critical) → Claude is woken by `ahub watch`
  (Monitor) / `ahub wait`
accept.py: ahub accept (scout — accept; code — merge --no-ff into the work branch, acceptance, roll back if red, push,
  copy cleanup, archive), rework / reject / continue / task edit / extend / budget / model
```
States and transitions — `ahub/model.py` (the single source of the names); transitions and the lease —
`ahub/transitions.py`.

## `ahub/` modules
| Module | Role |
|---|---|
| `cli.py`, `cliutil.py`, `commands/*.py` | CLI: auto-discovery via `register()`; `--json`; errors — one line, code 2; `--lang {en,ru}` (command output) |
| `i18n/` | EN/RU string catalog: `t(key, **kw)`, `en.py`/`ru.py` (the key order is the same); language — `AHUB_LANG` → `lang` in the config → `LANG`/`LC_ALL`/`LC_MESSAGES` (`ru*`) → `en` |
| `config.py`, `paths.py` | `.hub.toml` v2 (v1 is read with a translation), `~/.config/ahub/config.toml` ([telegram] token/chat/proxy, [usage] the Go limit, [paths] opencode/claude/opencode_db, [providers.<name>] one table, one parser loop (`_provider_settings`): `enabled` — the one place a provider is on/off (`HubConfig.provider_enabled`, `providers_off`), and the advanced keys — proxy/no_proxy, the provider's own proxy (`HubConfig.provider(name)`; per key: absent — inherited, "" — explicitly none, a value — set) and sandbox — the codex OS sandbox mode, absent — the provider's default); data `~/.local/share/ahub/ahub.db`, logs `~/.local/state/ahub/logs`; under `AHUB_HOME` everything is in one directory, the global config too (`AHUB_HOME/config/config.toml`) — an isolated instance does not read the real one; there is no fallback to the v1 config (`~/.config/agent-hub`); `ProjectConfig` has `python_bin()`: explicit `python` → the project venv (`.venv`, `venv`) → `python3` |
| `store.py` + `migrations/` | SQLite WAL: task, task_dep, session, event (delivery/ack), question, message, draft, model/role_model, presence, claude_launch, observer_report, tg_chat, op |
| `model.py`, `transitions.py` | types, states, transitions, events; move/acquire/renew/release/request_stop/once |
| `tasks.py`, `drafts.py` | task creation with checks; a draft in plain words → model → preview → launch |
| `registry.py` | models (alias → provider/model/variant), role menus, project bans, free/paid/plan (`cost_kind`); a provider switched off in the hub config leaves every menu, and naming such a model in a task is a refusal with the way out |
| `providers/` | `base.py` contract; `runner.py` shared run (output to a file — opencode drops the tail into a pipe; silence with children counted; stop by group; the process env — scrubbed + the provider's own proxy, `runner.hub_proxy()`); `opencode.py`, `opencode_db.py`; `agy.py` (Antigravity/Gemini: `-p … --output-format stream-json`, session id from `init.conversation_id`, `--dangerously-skip-permissions`, usage without money — window quota; no export/find_session/prices); `codex.py` (OpenAI Codex CLI: `codex exec --json` / `exec resume <id>`, session id from `thread.started.thread_id`, tokens from `turn.completed` and no money — a ChatGPT subscription, catalog from `codex debug models`, `-c approval_policy="never"` (no waiting for a human) + `-s <sandbox>` (the OS sandbox, Landlock/bwrap on Linux and Seatbelt on macOS; the mode from `[providers.codex] sandbox`, default workspace-write; `codex sandbox <mode> -- true` checks it in health(), a mode without a sandbox is not probed; the fix on a failure — `doctor.codex_sandbox_fix`: user namespaces (AppArmor on Ubuntu 24.04) or danger-full-access); `fake.py` for tests |
| `workspace.py`, `prepare.py`, `gates.py`, `review.py`, `prompts.py` | copy/branch, preparation (`scrub_env` — no hub secrets, `apply_proxy` — the provider's own proxy), gates, review panel, prompts |
| `engine.py`, `worker.py` | the turn of a task, the process of a task |
| `service.py` | queue, orphans, self-update onto new code, heartbeat, observer thread |
| `events.py`, `comms.py`, `views.py`, `archive.py` | delivery/presence; messages/questions/alarms; L1–L3 with limits; archive `<project>/.agent-hub/` |
| `pulse.py`, `observer.py` | pulse 🟢🟡🔴⚫⚪; observer (5 min code, 30 min model, the Koala proxy, escalation) |
| `doctor.py`, `commands/doctor.py` | `ahub doctor`: installation check (what is wrong → what to do; a provider with its own proxy shows it and whether it answers; a codex sandbox that does not start gives both fixes — `apparmor_blocks_userns()` reads /proc, `codex_sandbox_fix()` builds the hint, the hub changes no system settings); the `ahub setup` wizard and `ahub providers` reuse the checks — `provider_states()` (found / logged in / a note / an install hint per provider), `probe_models()` (several live probes at once), `recommend_model()` (a paid answerer, else a free one) |
| `commands/setup.py` | the `ahub setup` wizard: language, project, **providers** (every provider, found / logged in / note / install hint; which to enable — the default is found and logged in, `set_provider_enabled` writes `[providers.<name>] enabled`), **models** (a live probe of every model of a provider that is on, ✓/✗ with free/paid/plan, the default of executor and reviewer is picked — Enter takes the recommendation; the other roles follow the executor unless their own default works), service, Claude skill, Telegram, the summary through `doctor.run_all()`; a TTY without `--yes` — interactive, otherwise the same choices by the rule, printed as a summary; `AHUB_PROBE=0` — no probe, a free alias as before |
| `commands/providers.py` | `ahub providers`: the table (name, found, logged in, enabled, models + notes and hints), `ahub providers enable\|disable <name>` — the same switch the wizard writes |
| `tg/` | the bot (`core.py` logic, `run.py` aiogram, `launcher.py` launching Claude without a live session, `proxy.py`) |
| `tui/` | `ahub top` (`data.py` data, `app.py` textual) |
| `mcp.py` | MCP server (stdio) over the same handles |
| `claude/SKILL.md` | the skill for Claude Code (installed by `ahub setup --claude`) |

## Processes
- The OS service (`ahub service install`): Linux — systemd --user units `ahub.service` + `ahub-bot.service`
  (`commands/service.py`: `ExecStart=<python> -m ahub service|bot run`, `Restart=always`, `KillMode=process`);
  macOS — launchd plists `dev.ahub.service.plist` + `dev.ahub.bot.plist` in `~/Library/LaunchAgents`
  (`Label`, `ProgramArguments`, `RunAtLoad` + `KeepAlive`, the same environment, logs in `state/logs`;
  enabling — `launchctl bootstrap gui/$(id -u) <path>`). The bot unit/plist only when Telegram is on
  (`[telegram] token`), otherwise the line "the bot is not installed: no [telegram] token".
- Without an OS service: `ahub service start` — `service run` in the background (`start_new_session`, log
  `state/logs/service.log`, the pid in `service_pid_path()` of the data dir); a live pid or a heartbeat tick < 30 s —
  no second one is started. `ahub service stop` — SIGTERM by the pid file, wait up to 10 s, remove the file.
- Task processes are separate (`python -m ahub.worker T<id>`), they survive a service restart.
- `ahub/procs.py` — children, liveness, cmdline, start time: Linux via /proc, otherwise psutil.
- Claude: Monitor on `ahub watch`; `ahub status`; decisions — `ahub accept|rework|reject`.

## Working with the code
- Tests: `.venv/bin/python -m pytest -q` (~2.5 min; HOME is faked, no network); live: `AHUB_LIVE=1 … -m live`.
- Style: py3.11+, `from __future__ import annotations`, dataclasses, stdlib sqlite3, time as a parameter,
  comments and docstrings in English.
- Tasks for agent-hub itself can also be run through the hub (`.hub.toml`: work branch `main`).