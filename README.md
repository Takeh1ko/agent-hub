# agent-hub

[Русская версия](README.ru.md)

Let Claude Code hand work to cheap models — and only spend its own tokens on decisions.

agent-hub is a local service between an orchestrator (Claude Code, or any CLI agent) and worker models
(opencode: Muse Spark, free models; Gemini via Antigravity). Claude files a task, goes quiet, and is woken by
one line when there is something to decide: accept, send back with notes, or reject. Everything in between —
running the worker in its own copy of the repo, checking its work, review by other models, retries, budgets —
the hub does without Claude.

![agent-hub: a task goes from Claude to a cheap model, through gates and review, and is merged](docs/demo.gif)


## Why

- **Claude's context is the expensive part.** A delegated task costs Claude about 2 KB of text for the whole
  cycle: one event line, a summary capped at 4 KB, a one-word decision. Reports, diffs and logs are there on
  request, never pushed.
- **Cheap models are good enough when the work is checked.** A task is not "done" because the model says so:
  the commit exists, the diff stays inside the allowed files, the acceptance tests pass under a project lock,
  and one or more reviewer models (in fresh sessions) agree. Typical costs on Muse Spark: recon $0.004–0.06,
  code with review $0.02–0.13.
- **Nothing gets lost.** Events are stored until acknowledged, so a restarted Claude session, a restarted hub or a
  dead terminal does not drop a "done". Worker processes outlive service restarts; orphaned tasks are picked up
  again; an observer watches the hub itself.

## What it does

| | |
|---|---|
| Task kinds | `scout` (report only), `code` (branch + gates + review), `routine` (light changes), `review` (of a branch/commit/range) |
| Isolation | a git worktree per task, secrets hidden from the copy, clean environment for the worker |
| Gates | commit present, diff ⊆ allowed paths, structured result, acceptance tests under a shared lock |
| Review | panel of reviewer models in new sessions; disputes count only with file, line and reason; N rounds |
| Merge | `ahub accept` merges `--no-ff`, re-runs acceptance, rolls back if red |
| Failures | network error → retry; silence → one nudge; quota/timeout/budget → "needs decision"; never a silent hang |
| Budgets | per task (including review); at 100 % the worker is asked to save and stop, extension is one command |
| Waking Claude | `ahub watch` for Claude Code's Monitor, `ahub wait`; stable codes `DONE` `DECISION` `ERROR` `OWNER` `ANSWER` `ALARM` |
| Pulse | 🟢 working · 🟡 waiting for a reason · 🔴 silent · ⚫ dead · ⚪ no data — from the provider, processes and locks |
| Observer | code checks every 5 min, a model review every 30 min, escalation to Claude, then to you |
| Human | `ahub top` terminal UI; optional Telegram bot that talks to Claude (and starts Claude if no session is live) |
| Other agents | the same handles over MCP (`ahub mcp`) |
| Languages | English and Russian (`AHUB_LANG`, `lang` in config, or the locale) |

## How it compares

Several projects connect Claude Code to other agents. Most are thin bridges (start a session, poll its status).
agent-hub is narrower in scope and deeper in the task lifecycle:

| | agent-hub | thin MCP bridges (opencode-mcp, agent-delegation-mcp, agy bridges) | fleet orchestrators (claw-orchestrator, Composio agent-orchestrator) |
|---|---|---|---|
| Focus | Claude decides, cheap models work | pass a prompt, return output | many agents in parallel, dashboards, PR loops |
| Gates and acceptance tests before "done" | yes | no | partly |
| Review by other models | yes | no | yes |
| Orchestrator token budget as a design rule | yes (hard size limits) | no | no |
| Events survive restarts (ack) | yes | no | partly |
| Observer of the hub itself | yes | no | no |
| Web dashboard | no (terminal UI + Telegram) | no | yes |
| Number of supported agents | opencode, agy | one | many |

If you want a dashboard for twenty parallel agents with PR automation, use a fleet orchestrator. If you want
Claude Code to stop burning its context on work a cheaper model can do — and to trust the result — this is it.

## Install

Requires Python 3.11+, git, and at least one provider: [opencode](https://opencode.ai) (free models work) or
Google Antigravity CLI (`agy`).

```
pipx install git+https://github.com/Takeh1ko/Midas-AI-HUB
ahub setup                 # language, project, providers, models, service, Claude skill, Telegram (optional)
ahub doctor                # what is wrong and how to fix it
```

`ahub setup --yes` takes all defaults without questions. Telegram is optional: `pipx install 'ahub[telegram] @ git+https://github.com/Takeh1ko/Midas-AI-HUB'`.

Platforms: Linux (systemd), macOS (launchd; tested in CI), Windows via WSL2 only.

## Use

In Claude Code (after `ahub setup` installs the skill):

```
ahub task new --kind code --title "add retry to the payment client" \
  --spec-file spec.md --paths "app/payments/**,tests/**" --accept "tests/test_payments.py"
```

Claude keeps a Monitor on `ahub watch`; when a line like `DONE T12 …` arrives it runs `ahub status T12` and decides:
`ahub accept T12`, `ahub rework T12 --notes "…"`, or `ahub reject T12`.

You: `ahub top` to watch (press `c` for control mode, `?` for help), `ahub status`, `ahub history`.
Plain-language tasks: `ahub draft new "what you want, in your words"` → preview → `ahub draft start N`.

Everything else: `ahub --help`.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — code map, start here
- [docs/architecture.md](docs/architecture.md) — design
- [docs/contracts.md](docs/contracts.md) — interfaces between the parts

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q          # ~2.5 min, no network, HOME is faked
AHUB_LIVE=1 .venv/bin/python -m pytest -m live   # real providers, costs money
```

## License

MIT
