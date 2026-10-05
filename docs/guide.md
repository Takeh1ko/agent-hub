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
8. **Doctor** — the full check of steps 1 through 7 as a summary.

Flags: `ahub setup --yes` takes every default without questions, `--claude` installs the Claude Code part without
asking, `--service` installs and enables the OS service, `--name` and `--deny` set the project name and the models
denied in it, `--lang` writes the hub language.

`ahub doctor` runs the same checks on demand: every line says what is wrong and what to do about it, and it exits 1
if something is broken. `ahub --json doctor` gives the machine form.

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
ahub models                         # the menu of every role, ★ = the default of the role
ahub models --all                   # every alias with its provider and model id
ahub models role reviewer --add mimo-flash --default
ahub models add mymodel --provider opencode --model-id opencode-go/mimo-v2.6-flash
ahub models check                   # one tiny live request per role default
```

Roles:

| Role | What it does |
|---|---|
| `executor` | works on `code` tasks: writes code and tests in the task copy, commits |
| `reviewer` | the review panel: fresh sessions, one verdict each, disputes and fixes — and `review` tasks |
| `scout` | `scout` tasks: reads and reports, changes nothing |
| `routine` | `routine` tasks: light file work, no acceptance tests |
| `observer` | watches the hub itself: a code check every 5 min, a model review every 30 min |
| `drafter` | turns a plain-language description into task fields (`ahub draft`) |

How money works per provider: opencode free models need no login and cost nothing; opencode Go models need
`opencode auth login` and are billed per token; `agy` spends the quota of your Google account; codex spends your
ChatGPT plan. The last two report token counts and no price, so a task on them shows `$0.000 Go` and its tokens
appear in `ahub follow`.

`agy` reads its quota windows from `/usage` (the Gemini 5h and weekly buckets) and the hub schedules around them:
below `[quota] min_5h` / `min_weekly` a task waits for the reset or starts on the fallback model (`fallback`,
`fallback_executor`, `fallback_reviewer`). `ahub providers` and `ahub doctor` show every bucket; a quota error
mid-task returns it to the queue with the wait reason instead of `needs_decision`.

## Running tasks

```
ahub task new --kind code --title "add retry to the payment client" \
  --spec-file spec.md --paths "app/payments/**,tests/**" --accept "tests/test_payments.py"
```

| Kind | Result | Gates |
|---|---|---|
| `scout` | a report | the report exists and fits the shape; nothing changed |
| `code` | a branch + a report | a commit exists, the diff ⊆ `--paths`, acceptance is green under the project lock |
| `review` | findings (of a branch, a commit, a range or files) | no gates; every reviewer submits a verdict, nothing changed |
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

## Prompts

Every worker or reviewer session receives guidance assembled in layers, followed by the task specification and the built-in hub layer:

1. **User guidance** (optional Markdown files, one per role):
   - **Global** — `~/.config/ahub/prompts/<role>.md` (`AHUB_HOME/config/prompts/`) — applies to all projects;
   - **Project** — `<repo>/.hub/prompts/<role>.md` — committed to git, shared across contributors;
   - **Local** — `~/.config/ahub/projects/<project-name>/prompts/<role>.md` — personal overrides, not in git.

   Roles: `all` (prepended to every session), `code`, `routine`, `scout`, `review` (for reviewer panel sessions and `review` kind tasks).
   Assembly order: scope-major — global (`all.md`, then `<role>.md`), then project (`all.md`, then `<role>.md`), then local (`all.md`, then `<role>.md`). Within each scope `all.md` precedes `<role>.md`, and later scopes refine earlier ones. Each non-empty scope appears under its heading (`## Global guidance`, `## Project guidance`, `## Local guidance`).
2. **The task specification** — title, description, files to read first, and expected results.
3. **Built-in hub layer LAST** — worktree boundary isolation, secrets protection, the short quality bar (code/routine tasks), submission contract (`.ahub/result.json`, report format, commit rules), and reply language. User guidance cannot override these constraints.

Example `<repo>/.hub/prompts/review.md`:

```markdown
- blocker: any SQL built with string formatting; require parameterization.
- blocker: broad `except Exception` without logging or re-raising.
- taste / nit: prefer descriptive variable names over single letters.
```

Commands:

```
ahub prompts                      # table of prompt layers applying to the current project
ahub prompts show code            # assembled prompt exactly as the model receives it (--json for parts)
ahub prompts edit review          # open in $EDITOR, or create from template (default: project)
ahub prompts edit scout --global  # edit global guidance
ahub prompts edit code --local    # edit local project guidance
ahub prompts check                # inspect sizes (>4 KB warn, >16 KB refuse), unknown files, legacy rules
```

`ahub status T12` shows the prompt layers used in a dim line (e.g. `prompts: built-in + global(code) + project(all, code)`). `ahub doctor` checks prompts directory health as well.
Back-compat: `rules = "…"` in `.hub.toml` continues to work as project `all.md` if `.hub/prompts/all.md` is absent (`ahub prompts check` suggests migrating).

## Watching

```
ahub status                # L1: one line per task, byte-budgeted (`ahub top` for the rest)
ahub status T12            # L2: state, model, cost, summary, the next commands
ahub result T12            # L2, the same task view as ahub status T12; --full — result.json and the whole report (L3)
ahub diff T12              # the diff from the base
ahub follow T12            # live readable transcript: prompts, text, tool calls, results
ahub log T12               # the raw session log
ahub history -n 20         # recent tasks with their outcome and cost
ahub top                   # the interactive console: tasks, pulse, money, events
ahub                       # with no args on a TTY — the same console as `ahub top`
```

`ahub` with no arguments opens the console when stdin and stdout are TTYs (a pipe or `--json` keeps
the one-shot output byte-identical); `ahub top` opens the same console:

```
╭──────────────────────────────╮
│ ✻ ahub 3.0.0 · demo          │
│ project: demo · /srv/demo    │
│ service running (tick 3s ago)│
╰──────────────────────────────╯
⏺ ✢ T12  add retry to payments
  ⎿ writing code · spark · 2m 13s
◦ T13  queued · waiting for T12
⏺ T11  waiting for you
  ⎿ Next: ahub accept T11 …
> /accept T11
```

Stages read at a glance: `⏺` white + spinner — active (`writing code`, `studying`, `running tests`,
`in review`); `◦` dim — queued; `⏺` yellow — waiting for you; `✗` red — error/dead; `⏸` dim — stopped.

Real `ahub status` output (a `scout` and a reviewed `code` task on the free `bunny` model):

```
$ ahub status
repo · 0 active · 2 waiting · 0 queued
Waiting
  T2  done  report ready
  T3  done  review: all agree

$ ahub status T3
T3  code  add a double() helper next to retry()
───────────────────────────────────────────────
State  done · review: all agree
Model  bunny  Review  bunny
Cost   $0.000 Go of $1.50 budget
Age    2 min
Summary
  Added a double(x) helper to lib.py alongside the existing retry(fn), plus unit tests in
  tests/test_lib.py covering zero, positive, negative and float inputs. The pre-existing test_ok and
  a retry regression test are kept, and all three tests pass under pytest.
Open points
  No additional files were needed; everything requested fit inside the allowed paths (lib.py,
  tests/**). tests/test_lib.py prepends the repository root to sys.path before importing lib so the
  test passes regardless of the working directory pytest is launched from, because there is no
  root-level conftest.py and adding one would be outside the allowed file list.…
Next  ahub accept T3 · ahub rework T3 --notes "…" · ahub reject T3
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

Quota windows are not money: a Gemini task waits (`waiting for Gemini quota: 5h 12%, resets 17:16`) or moves to the
fallback model, and one quota group is shared by at most `ceil(remaining_5h * 6)` tasks at once. A quota error
mid-turn requeues the task — it restarts in a fresh session by itself after the reset (the session that hit the
error is abandoned: resuming it would repeat the error).

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
| A task is refused with `no model 'spark'` or `provider opencode is off` | `ahub models --all` for the list of aliases; `ahub providers enable opencode` for a provider that is switched off |
| A free model stopped answering (rate limit, gone from the catalog) | `ahub models check`, then `ahub models role <role> --set-default <alias>` |
| A task sits in one state for too long | `ahub follow T12` to see what the worker does; `ahub nudge T12 "…"` to steer it; `ahub stop T12` and `ahub continue T12` if it must restart |
| codex fails every command silently on Ubuntu 24.04 | `ahub doctor` names it; either allow the user namespaces or set `sandbox = "danger-full-access"` |
| A provider is installed but not logged in | `ahub doctor` shows the exact command: `opencode auth login`, `agy` (Google account), `codex login` |
| Nothing runs at all | `ahub service status` — the queue needs a live service; `ahub service start` without an OS service |
| An event was never seen by the orchestrator | it is not lost: `ahub inbox`, `ahub status`, and `ahub ack` only after the decision |
