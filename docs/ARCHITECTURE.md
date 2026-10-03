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
  `ahub nudge T12 "…"` (a request on the task, like request_stop) → the turn is interrupted and the SAME session
  continues with the text (round, budget, gates untouched); quota/timeout/budget → needs_decision; error → error;
  stop → stopped
  every turn: the prompt → .ahub/logs/<name>.log.prompts.jsonl, the raw stream → .ahub/logs/<name>.log
  (`ahub follow T12` — transcript.py reads both)
events.py: codes DONE/DECISION/ERROR/OWNER/ANSWER/ALARM (ALARM! — critical) → Claude is woken by `ahub watch`
  (Monitor) / `ahub wait`
accept.py: ahub accept (scout — accept; code — merge --no-ff into the work branch, acceptance, roll back if red, push,
  copy cleanup, archive; the branch is already in the work branch — an interrupted accept: the gates and the merge are
  skipped, acceptance runs on HEAD, then the same tail), rework / reject / continue / task edit / extend / budget / model
`ahub nudge` (MCP tool `nudge`, `ahub top` key `m`): only a task whose process runs the worker (a live lease, a known
  session id) — otherwise one line, code 2; the event `nudge` is a journal entry, not a wake-up for Claude
```
States and transitions — `ahub/model.py` (the single source of the names); transitions and the lease —
`ahub/transitions.py`.

## `ahub/` modules
| Module | Role |
|---|---|
| `cli.py`, `cliutil.py`, `commands/*.py` | CLI: auto-discovery via `register()`; `--json`; errors — one line, code 2; `--lang {en,ru}` (command output) |
| `i18n/` | EN/RU string catalog: `t(key, **kw)`, `en.py`/`ru.py` (the key order is the same), `template(key)` — the raw template of a key (the reason renderer formats it itself); language — `AHUB_LANG` → `lang` in the config → `LANG`/`LC_ALL`/`LC_MESSAGES` (`ru*`) → `en` |
| `ui.py` | the one rendering layer: `styled()` (colour only when stdout is a TTY and `NO_COLOR` is unset — a pipe, i.e. Claude, always gets plain compact text), `badge()`, `rule()`, `section()`, `kv()` (aligned label/value; a value may be the chunks of the line), `para()`/`bullets()` (wrap at word boundaries, paragraphs and list items kept), `table()` (columns from the content, capped and shrunk to the width, an ellipsis in a cell only), `fit()` (cut to a byte budget at a paragraph/sentence end + a hint where the rest is); the width is passed in or taken from `COLUMNS`/the terminal (fallback 100) |
| `reasons.py` | a state reason is data, not text: the hub stores `{"code": "wait_accept", "task": "T53", "state": "reviewing"}` (`dump`, every param capped at `PARAM_LIMIT`) and every reader renders it through the `reason.*` catalog keys at read time (`text`) — a row written in Russian reads correctly in English; a reason with no code (an old row, a human note from `ahub reject --reason`) is shown as it is. Every writer of `task.state_reason` goes through it: `transitions.move`, `service` (the queue wait reasons, the orphans), `accept`, `engine` (incl. `review.panel`); a gate problem is stored as a sub-reason of its reason (`gates.Problem` carries both the prompt text and the code) |
| `config.py`, `paths.py` | `.hub.toml` v2 (v1 is read with a translation), `~/.config/ahub/config.toml` ([telegram] token/chat/proxy, [usage] the Go limit, [paths] opencode/claude/opencode_db, [providers.<name>] one table, one parser loop (`_provider_settings`): `enabled` — the one place a provider is on/off (`HubConfig.provider_enabled`, `providers_off`), and the advanced keys — proxy/no_proxy, the provider's own proxy (`HubConfig.provider(name)`; per key: absent — inherited, "" — explicitly none, a value — set) and sandbox — the codex OS sandbox mode, absent — the provider's default); data `~/.local/share/ahub/ahub.db`, logs `~/.local/state/ahub/logs`; under `AHUB_HOME` everything is in one directory, the global config too (`AHUB_HOME/config/config.toml`) — an isolated instance does not read the real one; there is no fallback to the v1 config (`~/.config/agent-hub`); `ProjectConfig` has `python_bin()`: explicit `python` → the project venv (`.venv`, `venv`) → `python3` |
| `store.py` + `migrations/` | SQLite WAL: task, task_dep, session, event (delivery/ack), question, message, draft, model/role_model, presence, claude_launch, observer_report, tg_chat, op; a row becomes a dataclass by its own fields only — a column a newer schema added is dropped, so a process on the old code survives a migration under it |
| `model.py`, `transitions.py` | types, states, transitions, events; move/acquire/renew/release/request_stop/request_nudge (message into the working session; the owner takes it back with clear_request)/once |
| `tasks.py`, `drafts.py` | task creation with checks; a draft in plain words → model → preview → launch; the project test resource is NOT added to a code task's resources (acceptance takes its lock itself, gates) — `--resources` for whole-task exclusivity |
| `registry.py` | models (alias → provider/model/variant), role menus, project bans, free/paid/plan (`cost_kind`); a provider switched off in the hub config leaves every menu, and naming such a model in a task is a refusal with the way out |
| `providers/` | `base.py` contract; `runner.py` shared run (output to a file — opencode drops the tail into a pipe; silence with children counted; stop by group (`request_stop()` — every live run by group, from a signal handler); `PollFailed` — a `should_stop()` that cannot poll: the group is killed, the error is let out; the process env — scrubbed + the provider's own proxy, `runner.hub_proxy()`); `opencode.py`, `opencode_db.py`; `agy.py` (Antigravity/Gemini: `-p … --output-format stream-json`, session id from `init.conversation_id`, `--dangerously-skip-permissions`, usage without money — window quota; no export/find_session/prices); `codex.py` (OpenAI Codex CLI: `codex exec --json` / `exec resume <id>`, session id from `thread.started.thread_id`, tokens from `turn.completed` and no money — a ChatGPT subscription, catalog from `codex debug models`, `-c approval_policy="never"` (no waiting for a human) + `-s <sandbox>` (the OS sandbox, Landlock/bwrap on Linux and Seatbelt on macOS; the mode from `[providers.codex] sandbox`, default workspace-write; `codex sandbox <mode> -- true` checks it in health(), a mode without a sandbox is not probed; the fix on a failure — `doctor.codex_sandbox_fix`: user namespaces (AppArmor on Ubuntu 24.04) or danger-full-access); `fake.py` for tests |
| `workspace.py`, `prepare.py`, `gates.py`, `review.py`, `prompts.py` | copy/branch, preparation (`scrub_env` — no hub secrets, `apply_proxy` — the provider's own proxy), gates (`gates.Problem` — the prompt text plus the code; `RESULT_JSON_CODES` — the problems an orchestrator's own edit may cause), review panel, prompts |
| `engine.py`, `worker.py` | the turn of a task, the process of a task; one cooperative poll of the owner request feeds two predicates: `stop_requested` (lease lost, budget, a stop request — the gates and the reviewer sessions, which carry the result of the turn, wait through a nudge) and `interrupt_requested` (also a nudge — a worker turn is interrupted, `session(stop_predicate=…)` chooses); the message is delivered as one more turn of the same session (prompts.py `nudge_prompt`, kind `nudge`); the poll is guarded — 3 failures in a row (old code, newer schema) → one log line, `PollFailed` kills the provider group, the worker exits 4 and the service re-picks the task as an orphan on the current code |
| `transcript.py`, `commands/follow.py` | readable transcript of a session: before every turn the engine writes the prompt into the sidecar `<log>.prompts.jsonl` (`{"ts", "turn", "kind", "text"}`, kind — start/continue/repair/rework/stop/review/nudge), `transcript.Reader` reads that sidecar plus the raw log through the provider's parse_line (the provider comes from the session row) and `Writer` renders prompt/turn headers, text, reasoning, tool call (▶ run / ✎ edit / 👁 read / 🔎 search), result, error, usage per step; `ahub follow T12 [--role] [--round] [--full] [--no-follow]` prints it and follows the log until the task leaves an active state (ANSI colors only on a TTY), `ahub log T12` stays raw; turns are matched to prompts by order — by the prompt time for a provider with stamped events (`Provider.stamped`, opencode), by the session id each run starts with otherwise (agy, codex) |
| `service.py` | queue, orphans (an interrupted acceptance → "needs decision" with the way out: `ahub accept T12` — that sentence is the `reason.orphan_accepting` code, rendered at read time), self-update onto new code, heartbeat, observer thread; `hub_env()` — a process the hub starts (a task worker, the self-update check) gets this hub on `PYTHONPATH`, so it always runs the code that started it; a live task process is only the `python -m ahub.worker T<n>` shape (`_worker_task`) — a shell command, a grep or an agent prompt that merely mentions the mark is not one |
| `events.py`, `comms.py`, `views.py`, `archive.py` | delivery/presence; messages/questions/alarms; a wakeup line clips with `ui.clip` and renders a stored reason through `reasons.text` (no JSON in an event line); the human views through `ui.py` (L1 `status` — the byte budget cuts after whole task rows and counts the rest (`+N more`, `ahub top`) — L2 `status T12`/`result`, where the fact lines put the pairs that belong together on one aligned line: Model/Review/Round, Age/After — `history`, `questions`, `inbox`) with the L1–L3 limits of contracts §5 — a cut block says where the rest is (`ahub result T12`); `--json` keeps the raw reason and adds the rendered `state_reason_text`/`reason_text`; archive `<project>/.agent-hub/` |
| `pulse.py`, `observer.py` | pulse 🟢🟡🔴⚫⚪; observer (5 min code, 30 min model, the Koala proxy, escalation) |
| `doctor.py`, `commands/doctor.py` | `ahub doctor`: installation check (what is wrong → what to do; a provider with its own proxy shows it and whether it answers; a codex sandbox that does not start gives both fixes — `apparmor_blocks_userns()` reads /proc, `codex_sandbox_fix()` builds the hint, the hub changes no system settings); the `ahub setup` wizard and `ahub providers` reuse the checks — `provider_states()` (found / logged in / a note / an install hint per provider), `probe_models()` (several live probes at once), `recommend_model()` (a paid answerer, else a free one) |
| `commands/setup.py` | the `ahub setup` wizard: language, project, **providers** (every provider, found / logged in / note / install hint; which to enable — the default is found and logged in, `set_provider_enabled` writes `[providers.<name>] enabled`), **models** (a live probe of every model of a provider that is on, ✓/✗ with free/paid/plan, the default of executor and reviewer is picked — Enter takes the recommendation; the other roles follow the executor unless their own default works), service, Claude skill, Telegram, the summary through `doctor.run_all()`; a TTY without `--yes` — interactive, otherwise the same choices by the rule, printed as a summary; `AHUB_PROBE=0` — no probe, a free alias as before |
| `commands/providers.py` | `ahub providers`: the table (name, found, logged in, enabled, models + notes and hints), `ahub providers enable\|disable <name>` — the same switch the wizard writes |
| `tg/` | the bot (`core.py` logic, `run.py` aiogram, `launcher.py` launching Claude without a live session, `proxy.py`) |
| `tui/` | `ahub top` (`data.py` data, `app.py` textual); in control mode `m` — a message to the worker (from the table and from the transcript screen, which has its own `m`), `M` — change model; the table keys wait behind the transcript screen (`TABLE_ONLY`) |
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
- Task processes are separate (`python -m ahub.worker T<id>`), they survive a service restart. SIGTERM/SIGINT to one:
  `runner.request_stop()` stops every provider run by group (and the other children it started), then the process exits —
  the task stays active, the service picks it up as an orphan (state untouched, no verdict written by a dying process).
- `ahub/procs.py` — children, liveness, cmdline, start time: Linux via /proc, otherwise psutil.
- Claude: Monitor on `ahub watch`; `ahub status`; decisions — `ahub accept|rework|reject`.

## Working with the code
- Tests: `.venv/bin/python -m pytest -q` (~2.5 min; HOME is faked, no network); live: `AHUB_LIVE=1 … -m live`.
- Style: py3.11+, `from __future__ import annotations`, dataclasses, stdlib sqlite3, time as a parameter,
  comments and docstrings in English.
- Tasks for agent-hub itself can also be run through the hub (`.hub.toml`: work branch `main`).