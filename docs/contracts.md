# agent-hub v2 — contracts between the parts

Interfaces that M2+ cards rely on. They change only deliberately (with an edit to this file).
Machine names of states/events/roles are in `ahub/model.py`; providers are in `ahub/providers/base.py`.

## 1. The task working copy

Every task (a scout too) works in its own copy of the project: a `git worktree` at `<worktrees>/T<id>`, branch
`<branch_prefix>T<id>` (by default `ahub/T12`) off the project's working branch. Scouts and reviews change no files —
the gates check that the copy is clean. The service directory in the copy is `.ahub/` (in the copy's
`.git/info/exclude`, not in the project's `.gitignore`):

| Path | Who writes | What |
|---|---|---|
| `.ahub/result.json` | worker | the outcome of the turn, in the shape of §2 |
| `.ahub/report.md` | worker | the report for humans/the orchestrator (required for a scout) |
| `.ahub/review_r<N>_<model>.json` | reviewer | a verdict, §3 |
| `.ahub/logs/<name>.log` | hub | the raw output of a session (`scout.log`, `executor.log`, `reviewer_r<N>_<model>.log`) |
| `.ahub/home/` | hub | an isolated hub storage inside the worker's session |
| `.ahub/prompt_*.md` | hub | a long prompt (> 60 KB) — the worker gets a reference to the file instead |
| `.ahub/schema_*.json` | hub | a JSON schema for `--output-schema` (codex) — the final answer must match it |

## 2. The worker's result — `.ahub/result.json`

```json
{
  "summary": "1–3 sentences: what was done / what was learned",
  "status": "done | blocked",
  "commit": "sha of HEAD (code/routine)",
  "files": ["changed files (code/routine)"],
  "tests": {"cmd": "…", "ok": true, "tail": "the last lines of the output"},
  "questions": ["what stayed unclear (short)"],
  "notes": "what is not done / open questions"
}
```
- Scout: `summary` + `.ahub/report.md` (≤ 12 KB, starting with a `## Summary` section ≤ 10 lines). No
  `commit/files/tests`.
- Review (a task kind): `summary` + a verdict in the shape of §3 in `.ahub/review_r1_<model>.json`.
- Code: `commit` == HEAD, `files` ⊆ the diff, `tests.ok` — the card's acceptance.
- Routine: like code, without `tests`.
- `status: blocked` — the worker cannot continue (no access, a contradiction in the assignment): the task → "Needs
  decision".

## 3. The reviewer's verdict — `.ahub/review_r<N>_<model>.json`

```json
{"verdict": "approve | changes | dispute",
 "findings": [{"severity": "high|medium|low", "file": "path", "line": 12, "issue": "≤ 300 chars", "fix": "what to do"}],
 "summary": "one sentence"}
```
- `dispute` counts only if every finding has `file`, `line` and an `issue` ≥ 50 chars; otherwise = changes.
- Panel: all approve → Done; any changes → rework (if rounds are left), otherwise "Needs decision".
- Findings are deduplicated by (file, line, normalized issue); `low` does not hold the task.

## 4. Events and delivery

A log event (`event`): `id, ts, task_id, project, kind, payload, needs_reaction, critical, delivered_at, acked_at`.

| kind | Wakes the orchestrator | How |
|---|---|---|
| done, needs_decision, error | yes | batched (grouping window) |
| owner_message, answer | yes | at once |
| alarm | yes | at once if `critical`; otherwise batched |
| the rest (state, phase, session, retry, silence, nudge, model_changed, budget_soft, orphan…) | no | the log only |

- The **grouping window** for non-critical ones is 120 s from the first unseen event (a hub-wide constant).
- **Delivered** (`delivered_at`) — the event was handed to the stream/wait. **Acknowledged** (`acked_at`) — the
  orchestrator took it. Delivered but not acknowledged does not disappear: after 30 min it is handed out again, at
  most 3 times in total (so it does not wake anyone with the same thing forever); after that it is visible in
  `ahub status` ("unread") and in the summary of the stream.
- **Implicit acknowledgement** (saves calls): reading a task (`ahub status T12`, `ahub result T12`) acknowledges its
  done/needs_decision/error; `ahub inbox` — owner_message/answer; `ahub alarms` — alarm.
  Explicit: `ahub ack <id…|all>`.

### Wakeup line (L0) — one per thing, ≤ 200 bytes
```
DONE T12 scout «find the leak» — report 2.1 KB; the leak is add(); $0.04
DECISION T13 code «the payment button» — review rounds are exhausted (2 findings, high: 1)
ERROR T14 code «migration» — prepare: the tests do not collect
OWNER «how is the payment going?»
ANSWER #5 «merge T12?» → yes
ALARM! opencode is unreachable for 12 min (3 tasks are waiting)
```
A `done` line carries what is there and drops what is not: the report size, the summary of `.ahub/result.json`
(clipped) and the money, in that order; the two kinds of money never become one number (`$0.04, $1.20 real`).
The codes at the start of a line are stable English and are never translated. Events written before the switch may
start with the old Russian words ГОТОВО/РЕШЕНИЕ/ОШИБКА/ВЛАДЕЛЕЦ/ОТВЕТ/ТРЕВОГА — the same events.

## 5. Detail levels and limits

| Level | Command | Limit | What |
|---|---|---|---|
| L0 | `ahub wait`, `ahub watch` | 200 B/line | §4 |
| L1 | `ahub status` | 1500 B total | a counts line (active / waiting for a decision / queued), the active tasks as a table, then what is not active (waiting and queued) under one heading, and the counters — open questions, unread messages, unread events; on overflow a group is cut after a whole row and the rest counted — `+5 more tasks — ahub top, ahub service status` |
| L2 | `ahub status T12`, `ahub result T12` | 4000 B | task: goal, state/reason, the worker's result (summary 900 B, open points, report 700 B), the review findings (deduplicated, `low` dropped, at most 6 that fit the block's own 1200 B), cost on one line, and the `Next` line — it is booked before the clip, so a full screen gives up the tail of a block, never the way out |
| L3 | `ahub result T12 --full`, `ahub diff T12`, `ahub log T12`, `ahub follow T12` | explicit; paged with `--max-bytes` (20000 by default; `ahub log` — 8000) | the full report, the diff, the session log |

All commands: `--json` — the same data machine-readable (no clipping of the text to the limit, the same set of
fields; a task view adds the raw reason next to its rendered `state_reason_text`).

## 6. Waiting and presence

- `ahub wait [--timeout 30m] [--project P | --all] [--who W]` — blocks until an event that needs a reaction
  (respecting the grouping window); prints L0 lines of unacknowledged events, marks them delivered; exit code 0.
  A timeout — empty output, exit code 3. A broken poll is retried (1 s apart) and a streak of the same error is one
  log line, not one per poll; after `MAX_POLL_FAILURES` (20) failures in a row the wait gives up with one line on
  stderr (`wait failed 20 times in a row: <error> — giving up`) and exit code 4. A poll that works resets the count.
- `ahub watch [--project P | --all] [--poll 3] [--who W]` — for the Monitor: an endless stream of L0 lines; the
  position is the delivered marks in the database (there is no state file: a Monitor restart neither loses nor
  repeats anything); at the start — one summary line about what is delivered but unacknowledged
  (`UNREAD 3: <lines>`; not the whole tail), remembered per consumer and per scope, so one project does not
  re-announce another's. The same give-up rule as `wait`, with the same exit code 4.
- **Presence**: `wait` and `watch` stamp `presence_project(who, project, last_seen, via, session_id)` at least once
  every 60 s — **one row per (who, project)**, because an orchestrator session works in one repository: Claude "is
  there" for a project when its row is younger than 180 s, and a live session in A says nothing about B. The owner's
  scope (`--all`, or a directory outside every project) stamps a row for every project of the hub config — the names
  are read once per `wait`/`watch`, not per stamp. The one-row-per-`who` table of before the migration (`presence`)
  is written too and read as a fallback, so a process on the previous code (a live reload) neither breaks nor looks
  absent; its row with an empty project is that code's owner-mode stream and counts for every project.
  The observer's escalation and launching Claude from Telegram are built on this.
- `who` is `claude` by default (`--who` for other orchestrators).

## 7. Orchestrator commands (CLI, M2)

Every command has its own `--help`, and `--json` gives the same data machine-readable (§5). Where a scope makes sense
there is `--project X` / `--all` (architecture §9); `ahub --help` groups the commands (Tasks, Watching, Setup, Models
and providers, Integrations). The pinned handles:

```
ahub task new --kind scout|code|review|routine --title "goal" (--spec "text" | --spec-file F)
              [--model spark] [--effort low|medium|high|xhigh|max] [--level 0-4]
              [--review "spark,mimo-flash" --rounds 2 | --no-review]
              [--paths "core/**,tests/**"] [--accept "tests/test_x.py::test_y"] [--read "…"] [--format "…"]
              [--budget 1.5] [--budget-usd 0] [--time-limit 30] [--after T3] [--resources test_db]
              [--input <branch|sha|a..b|files>] [--key K] [--draft] [--no-collect] [--by who]
   → two lines, see below; --key is idempotency (a repeat returns the same task), --draft stops at "T12 draft (…)"
   (--model accepts ALIAS[:EFFORT] and legacy spark-high/spark-medium/gemini-low, mapped with one line
   "spark-high is spark:high"; --effort overrides it, a mismatch is refused; unknown levels are refused
   listing the catalog's valid ones; the task stores alias + effort, status shows the level)
ahub status [T12]            L1 / L2 (L1 model column and L2 Model line show alias:effort, L2 adds the dim
                             identity "<display> · <plan> · <level>", e.g. spark + Muse Spark 1.3 · Go plan · xhigh)
ahub result T12 [--full]     L2 / L3
ahub diff T12 | log T12      L3 (the readable transcript of a session is `ahub follow T12 [--role] [--round] [--full]
                                       [--no-follow]` — it follows the log until the task leaves an active state)
ahub accept T12 | reject T12 [--reason] | rework T12 --notes "…" | continue T12 | stop T12 [--reason]
ahub nudge T12 "…" | task edit T12 [--spec|--spec-file|--title] [--review …] [--rounds N] [--model alias[:effort]]
ahub extend T12 --paths "…" | budget T12 --add N [--set N] | model T12 <alias[:effort]> (console: /model T12 alias[:effort])
ahub wait | watch | ack <id…|all> | inbox [<id>] [--peek] [--full] | questions [<id>] | alarms [--ack] [--acked]
ahub say "text" | ask "question" --options "yes,no" [--task T12]
ahub history [-n 20] | projects | cost [--project X|--all] [--since 30d] | doctor | top [--control]
ahub prompts | prompts show <role> [--json] | prompts edit <role> [--global|--local] | prompts check
ahub service {run,install,status,pause,resume,start,stop} | setup [path] | providers | draft "…" | bot run | mcp
ahub models [--role R] [--refresh] [--json] | models add … | models role R (--add ALIAS[:EFFORT] |
   --remove ALIAS[:EFFORT] | --set-default ALIAS[:EFFORT]) | models check | models enable|disable <alias>
   (the catalog table, grouped by provider: alias · model + vendor · reasoning · plan · price · context · roles,
   roles with the default effort next to each — executor:xhigh; plan column sized to "pay-as-you-go",
   prices with two decimals — $0.60 / $0.10; the provider header carries plan-level usage — Go spend of
   the month limit, agy quota windows; --role — that role's menu with the same columns and the effort
   in the alias cell — spark:high; it does not lift project bans; legacy spark-high/spark-medium/gemini-low
   are hidden from the tables but accepted with the mapping line)
```
`ahub projects` and `ahub cost` are the owner's glance at the whole hub — they do not follow the directory's scope;
`ahub cost` takes a scope and a period of its own.

### What a command prints

A command that changed something prints its result and, usually, a **second line** — the `Next` line: the commands
that follow for this task (picked by the state — a decision, a resume, the new task, the task itself). `inbox <id>`
and `questions <id>` read one row in full (the lists cut the text to a cell) and mark nothing read — only the inbox
list does. A refusal is one `error: …` line on stderr plus a `hint:` line with the way out when it is known.

```
$ ahub task new --kind code --title "add sub()" --paths "src/**" --accept "tests/test_app.py"
T2 queued (code, spark, review spark×2)
Next  ahub status · ahub follow T2

$ ahub status
webapp · 0 active · 4 waiting · 12 queued
Waiting
  T2   needs decision  gates still failing after the fix: no commit from the base
  T3   error           hub failure: IntegrityError: UNIQUE constraint failed: session.provider, session.external_id
  T7   queued
+5 more tasks — ahub top, ahub service status
open owner questions 1 · unread events 2

$ ahub status T2
T2  code  add sub()
───────────────────
State  needs decision · review rounds are exhausted (2 findings, high: 1)
Model  spark  Review  spark ×2  Round  2
Cost   $0.020 Go of $1.50 budget
Age    4 min
Summary
  added sub() to src/app.py
Review findings
  medium src/app.py:5
          sub() has no test, so a regression would not be caught
          fix: add a test for sub()
Next  ahub accept T2 · ahub rework T2 --notes "…" · ahub reject T2

$ ahub accept T6
T6 merged into main (9b2454976d)
Next  ahub status · ahub task new --kind scout --title "…"

$ ahub task new --kind code --title "x" --paths "src/**/*.py"
error: task not created: files src/**/*.py are outside the project-allowed ones (ahub/**, tests/**); acceptance (--accept pytest nodes) is required for a code task
  hint: ahub task new --help

$ ahub wait --timeout 5
OWNER «how is the payment going?>
ANSWER #5 «merge T12?» → yes

$ ahub inbox 12
#12
───
When     01:23
Project  webapp
  how is the payment going?

$ ahub projects
  project      path                     active  queued  decision  questions  go $   usd $  last
   webapp      /home/me/Projects/webapp     1       0         1           2  0.310  0.000  14:02
  ! other      —                           0       2         0           0  0.000  0.000  —
      project other has tasks but is not connected to the hub

$ ahub cost --all
  project  model  sessions  go $   usd $
  webapp   spark       12  0.310  0.000
all projects · since 2026-10-01
go $0.310 · usd $0.000 · sessions 12
```

Exit codes: 0 — success, 2 — refusal (`error: …` on stderr), 3 — a waiting timeout, 4 — `wait`/`watch` gave up
after `MAX_POLL_FAILURES` poll failures in a row (§6, `ahub/commands/comms.py`); the task process
(`python -m ahub.worker T12`): 0 — the task reached a decision, 2 — no task/project, 3 — held by another
owner, 4 — the owner poll keeps failing (the service re-picks the task as an orphan on the current code).

## 8. Project resources

`.hub.toml`: `max_parallel`, `[resources] name = {capacity, lock}`, `test_resource`, `[tests] args`
(extra pytest args for acceptance runs, e.g. `args = ["-n", "6", "--dist", "loadgroup"]`; default — no extra args).
A task declares the resources it
needs (`--resources`); the queue does not start a task while a resource is busy (busyness is the live task processes
with that resource; `lock` is an external flock whose holder is visible in the pulse). Waiting for a slot or a resource
is the phase `waiting` with a reason, not an alarm.
The `test_resource` is not added to a code task's resources: acceptance takes its `lock` only while it runs (gates), so
code tasks run in parallel and their acceptance runs wait for the lock one by one. Naming the resource in
`--resources` gives the whole task (the old behaviour; tasks already stored that way are unaffected).

## 9. Default models (the registry at first start)

| alias | provider / model / default level | roles (menu, effort next to each default) |
|---|---|---|
| spark | opencode / opencode-go/muse-spark-1.3-contributor / xhigh | executor★:xhigh, reviewer★:xhigh, scout★:xhigh, routine★:xhigh, observer★:high, drafter★:high |
| mimo-flash | opencode / opencode-go/mimo-v2.6-flash / — | executor, reviewer, routine |
| deepseek-flash | opencode / opencode-go/deepseek-v4.1-flash / high | executor, reviewer, scout |
| spark-free | opencode / opencode/muse-spark-1.3-contributor-free / xhigh | (outside the menu, available explicitly) |
| bunny | opencode / opencode/space-bunny-free / — | (outside the menu, available explicitly) |
| gemini | agy / gemini-3.8-flash-high / high | (outside the menu — chosen explicitly; window quota; :low picks the low sibling) |
| codex | codex / gpt-5.6-terra / — | (outside the menu — chosen explicitly; subscription + OS sandbox) |
| codex-fast | codex / gpt-5.6-luna / — | (outside the menu — chosen explicitly; subscription, cheaper/faster) |
★ — the default in the role (role menus store alias + effort: observer spark:high, drafter spark:high + spark).

Legacy spark-high/spark-medium/gemini-low are not seeded; an old hub maps their menu rows to
spark:high/spark:medium/gemini:low (migration 008, additive) and accepts them everywhere with one line
"spark-high is spark:high", hidden from the tables. One alias per model+plan: spark is the paid default
route, spark-free the free one.

Plans (`providers/base.py:PlanKind`, `registry.plan_kind`): free · Go plan (Go month limit) ·
pay-as-you-go (USD) · subscription (quota window). The catalog (`Provider.catalog() → CatalogEntry`:
model_id, display name, vendor, plan, prices per 1M, context, reasoning levels, status) enriches
every alias in `ahub models`, `ahub setup`, `ahub providers` and the console's /models. The price
column shows $ in / $ out per 1M whenever the catalog has a cost, whatever the plan (free → "free",
a subscription without prices → "—"); reasoning is the alias level plus the compact available range
("xhigh (minimal–xhigh)"), context is rounded ("200K", "1M"). The fake provider shows only with
`AHUB_FAKE_PROVIDER=1`.