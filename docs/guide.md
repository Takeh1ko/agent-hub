# agent-hub — user guide

[English](guide.md) · [Russian](guide.ru.md)

What the hub does in one paragraph: an orchestrator (Claude Code, or any CLI agent) files a task, the hub runs a worker
model on it in its own copy of the repository, checks the result, has other models review it, and wakes the
orchestrator when there is a decision to make. This guide is the task-oriented version of that: how to install, how
to choose models, how to file and watch tasks, what it costs, and what to run when something does not work.

For the code itself see [ARCHITECTURE.md](ARCHITECTURE.md).

## Install and first run

```
pipx install ahub
ahub setup
ahub doctor
```

`ahub setup` walks through eight steps:

1. **Language** — `en` or `ru`, written to the global config.
2. **Project** — the root of a git repository. The wizard creates `.hub.toml` (schema v2) with the fields you may
   want to change: `name`, `allowed_paths`, `worktrees`, `work_branch`, `python`, `[budget]`, `[timeouts]`, and
   registers the project in `~/.config/ahub/config.toml`.
3. **Providers** — every provider the hub knows (opencode, `agy`, `codex`): found or not, logged in or not, a note,
   and an install hint when it is missing. You choose which ones to switch on.
4. **Models** — a live probe (one tiny request per model) of every model of every provider that is on, marked
   free / paid / plan, and the defaults for the roles. `AHUB_PROBE=0` skips the probe.
5. **Service** — the OS service (systemd on Linux, launchd on macOS) or a background process.
6. **Claude Code** — the `ahub` skill, a block in the project `CLAUDE.md`, and the permission `Bash(ahub:*)`.
7. **Telegram** — optional, off by default; needs `pipx install 'ahub[telegram]'`.
8. **Doctor** — the full check of step 1 through 7 as a summary.

Flags: `ahub setup --yes` takes every default without questions, `--claude` installs the Claude Code part without
asking, `--service` installs and enables the OS service, `--name` and `--deny` set the project name and the models
denied in it, `--lang` writes the hub language.

`ahub doctor` is the same checks on demand: every line says what is wrong and what to do about it, and it exits 1 if
something is broken. `ahub --json doctor` gives the machine form.

The service runs the queue: `queued → preparing → working → checking → reviewing → … → done`, or
`needs_decision`, `error`, `stopped`. Without an OS service, `ahub service start` runs it in the background
(`ahub service stop`), and `ahub service status` shows the heartbeat, the queue and the task processes. Task
processes are separate and survive a restart of the service; a task whose process died is returned to the queue.

## Providers and models

```
ahub providers                      # the table: found, login, enabled, models
ahub providers enable opencode      # its models become selectable
ahub providers disable codex        # its models leave every role menu
```

A provider that is off is a hard switch: naming one of its models in a task is a refusal with the way out
(`ahub providers enable <name>`).

```
ahub models                         # the default model of every role
ahub models --all                   # every alias with its provider and model id
ahub models role reviewer --add mimo-flash --default
ahub models add mymodel --provider opencode --model-id opencode-go/mimo-v2.6-flash
ahub models check                   # one tiny live request per role default
```

Roles:

| Role | What it does |
|---|---|
| `executor` | works on `code` tasks: writes code and tests in the task copy, commits |
| `reviewer` | the review panel (fresh sessions) and `review` tasks |
| `scout` | `scout` tasks: reads and reports, changes nothing |
| `routine` | `routine` tasks: light file work, no acceptance tests |
| `observer` | watches the hub itself: a code check every 5 min, a model review every 30 min |
| `drafter` | turns a plain-language description into task fields (`ahub draft`) |

How money works per provider: opencode free models need no login and cost nothing; opencode Go models need
`opencode auth login` and are billed per token; `agy` spends the quota of your Google account; codex spends your
ChatGPT plan. The last two report token counts and no price, so a task on them shows `$0.000 Go` and its tokens
appear in `ahub follow`.

## Running tasks

```
ahub task new --kind code --title "add retry to the payment client" \
  --spec-file spec.md --paths "app/payments/**,tests/**" --accept "tests/test_payments.py"
```

| Kind | Result | Gates |
|---|---|---|
| `scout` | a report | the report exists and fits the shape; nothing changed |
| `code` | a branch + a report | a commit exists, the diff ⊆ `--paths`, acceptance is green under the project lock |
| `review` | findings | the findings fit the shape; nothing changed |
| `routine` | changes + a report | a commit exists, the diff ⊆ `--paths` |

Flags worth knowing: `--paths` (allowed files, comma-separated globs), `--accept` (pytest nodes that must pass),
`--model` (an alias, otherwise the role default), `--budget` (Go dollars for the whole task) and `--budget-usd`
(real money), `--after T3,T4` (start only after those tasks are accepted), `--review` / `--rounds` / `--no-review`
(the panel), `--time-limit` (minutes), `--input` (what a `review` task looks at: a branch, a sha, `a..b` or files),
`--resources` (make the task exclusive), `--draft` (file a draft instead of a task).

A task is validated before anything is paid for: the allowed files must be inside the project's `allowed_paths`,
the files to read must exist, the acceptance must collect, the model must be available. A refusal comes with the
reason.

In plain words, from a terminal:

```
ahub draft new "add retry to the payment client, tests in tests/test_payments.py"
ahub draft list
ahub draft start 1        # nothing runs until you start it
```

Decisions (all take the task label):

```
ahub accept T12                       # scout/report — accepted; code — merged --no-ff, acceptance re-run, rolled back if red
ahub rework T12 --notes "…"           # back to the worker, the same session, with your notes
ahub reject T12 --reason "…"          # dropped, the task copy is cleaned up (--keep keeps it)
ahub continue T12                     # after an error or a stop
ahub stop T12                         # ask the worker to wrap up
ahub extend T12 --paths "docs/**"     # allow more files
ahub budget T12 --add 1               # more money, a budget-blocked task resumes
ahub model T12 mimo-flash             # another model for the next round
ahub task edit T12 --spec-file spec2.md   # a new specification, in a new session
```

## Watching

```
ahub status                # L1: one line per task, byte-budgeted (`ahub top` for the rest)
ahub status T12            # L2: state, model, cost, summary, the next commands
ahub result T12            # the result in full (--full for the whole report)
ahub diff T12              # the diff from the base
ahub follow T12            # live readable transcript: prompts, text, tool calls, results
ahub log T12               # the raw session log
ahub history -n 20         # recent tasks with their outcome and cost
ahub top                   # the terminal UI: tasks, pulse, money, events
```

Real `ahub status` output:

```
$ ahub status
repo · 0 active · 1 waiting · 0 queued
Waiting
  T1  done   report ready
  T5  done   review: all agree

$ ahub status T5
T5  code  add a retry wrapper
─────────────────────────────
State  done · review: all agree
Model  fake  Review  fake
Cost   $0.096 Go of $1.50 budget
Age    0 min
Summary
  code: lib.retry retries once, test updated
Next  ahub accept T5 · ahub rework T5 --notes "…" · ahub reject T5
```

`ahub follow T12` prints the prompt and the turn as they happen and keeps following until the task leaves an
active state. `--role` picks the executor or the reviewer session, `--round` a session round, `--full` shows the
whole prompt and every result line, `--no-follow` prints once and exits.

`ahub nudge T12 "use the existing helper"` sends a message into the working session: the current turn is interrupted
and the same session continues with your text. It only works while the task's process is alive; otherwise it says so
and exits 2. In `ahub top` the same is `m` in control mode (`c`), `M` changes the model, `b` the budget, `o`
narrows the table to one project, `t` opens the transcript, `?` lists the keys.

The orchestrator's view:

```
ahub watch        # the event stream for Claude Code's Monitor
ahub wait         # blocking: returns when there is something or after --timeout
ahub ack 12 13    # acknowledge the events you have taken
```

Event lines start with a stable code: `DONE`, `DECISION`, `ERROR`, `OWNER`, `ANSWER`, `ALARM`. They are stored until
acknowledged, so a restart does not lose them. `ahub inbox`, `ahub questions`, `ahub ask`, `ahub say` and
`ahub alarms` are the human side of the same channel: a message from you becomes an `OWNER` event for the
orchestrator, a button question an `ANSWER`.

Telegram (optional, `pipx install 'ahub[telegram]'` and a token in the config): any text you send becomes a message
for Claude; if no Claude session is live for that project, the hub starts one. A message belongs to a project — the
project prefix in the text (`agent-hub: …`, `for agent-hub: …`), or the project you picked with `/project`;
`/tasks` shows the state read-only. The bot is installed as a second unit next to the hub service.

## Costs and budgets

The hub counts money the way the provider reports it: opencode sessions carry token counts and a price, so a task
shows `$0.096 Go of $1.50 budget`. `agy` and codex report tokens and no price, so their tasks show `$0.000 Go` and
the tokens per step in `ahub follow` — what they cost is the quota of the account.

* `--budget` is the Go budget of the whole task, review included. `--budget-usd` is the real-money budget (0 — no
  spending, by default).
* At 80 % the hub writes an event to the journal; at 100 % the worker is asked to save its progress and stop, and
  the task goes to `needs_decision` with the reason. Nothing is killed mid-write.
* `ahub budget T12 --add 1` raises the budget and the task resumes on its own.
* `ahub cost` shows the money of the hub's own sessions per project and per model (`--all`, `--since YYYY-MM-DD`).
  `ahub top` shows the same numbers in its header, per project group.

The README has real numbers from this repository's own tasks, with the tariffs that paid for them.

## Advanced: a proxy per provider, the Codex sandbox

By default every provider process inherits the hub's environment, so one system proxy (`HTTPS_PROXY` and friends)
applies to all of them. In `~/.config/ahub/config.toml` a provider can get its own — one through a proxy, another
direct:

```toml
[providers.opencode]
proxy = "http://127.0.0.1:8080"      # https_proxy/http_proxy/all_proxy for this provider's process
no_proxy = "localhost,127.0.0.1"

[providers.agy]
proxy = ""                            # empty string = explicitly no proxy (the inherited variables are dropped)
```

A key that is absent inherits the hub variable as it is, `""` means explicitly none (the variable is dropped), and a
value sets it — `proxy` covers `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY` and their lowercase twins, `no_proxy` covers
`NO_PROXY`/`no_proxy`, and the two keys do not affect each other. Only http/https/socks5/socks5h URLs are accepted.
The hub's own processes and the Telegram bot keep their behaviour (`[telegram] proxy` is separate). `ahub doctor`
shows each provider's own proxy and whether it answers.

The same section holds `sandbox` for Codex — the OS sandbox the worker runs in:

```toml
[providers.codex]
sandbox = "workspace-write"     # read-only | workspace-write (default) | danger-full-access
```

`workspace-write` lets the tools read anything but write only the task copy (the kernel enforces it). On Ubuntu 24.04
AppArmor blocks the unprivileged user namespaces bubblewrap needs, so codex then fails every command silently and the
turn comes out empty — `ahub doctor` detects this case and gives both fixes in one hint: allow the namespaces
(`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, plus a file in `/etc/sysctl.d/` to keep it — the
admin's decision) or set `sandbox = "danger-full-access"`, where the task copy and the acceptance gates hold codex as
they hold opencode and agy. The hub never changes system settings on its own.

## Language

`ahub --lang ru` (also `en`) switches the language of one command's output; `ahub setup --lang ru` writes it to the
global config as `lang = "ru"`. `AHUB_LANG=ru` overrides everything for one process. With neither, the locale decides
(`LANG`, `LC_ALL`, `LC_MESSAGES` starting with `ru`), otherwise English.

The worker prompts and the review panel are written in English so that any model understands them; the reports and
every human-readable field in a result follow the hub's language. Event codes are never translated — the Claude Code
skill parses them.

## Troubleshooting

| Symptom | What to run |
|---|---|
| Something in the installation is wrong, or a provider does not answer | `ahub doctor` — every line is a symptom and a fix |
| A task fails with "provider is off" or "no such model" | `ahub providers`, then `ahub providers enable <name>` |
| A free model stopped answering (rate limit, gone from the catalog) | `ahub models check`, then `ahub models role <role> --set-default <alias>` |
| A task sits in one state for too long | `ahub follow T12` to see what the worker does; `ahub nudge T12 "…"` to steer it; `ahub stop T12` and `ahub continue T12` if it must restart |
| codex fails every command silently on Ubuntu 24.04 | `ahub doctor` names it; either allow the user namespaces or set `sandbox = "danger-full-access"` |
| A provider is installed but not logged in | `ahub doctor` shows the exact command: `opencode auth login`, `agy` (Google account), `codex login` |
| Nothing runs at all | `ahub service status` — the queue needs a live service; `ahub service start` without an OS service |
| An event was never seen by the orchestrator | it is not lost: `ahub inbox`, `ahub status`, and `ahub ack` only after the decision |