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
| the rest (state, phase, session, retry, silence, budget_soft, orphan…) | no | the log only |

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
DONE T12 scout «find the leak» — report 2.1 KB, $0.04
DECISION T13 code «the payment button» — review rounds are over (2 high findings)
ERROR T14 code «migration» — preparation: the tests do not collect
OWNER «how is the payment going?»
ANSWER #5 «merge T12?» → yes
ALARM! opencode is unreachable for 12 min (3 tasks are waiting)
```
The codes at the start of a line are stable English and are never translated. Events written before the switch may
start with the old Russian words ГОТОВО/РЕШЕНИЕ/ОШИБКА/ВЛАДЕЛЕЦ/ОТВЕТ/ТРЕВОГА — the same events.

## 5. Detail levels and limits

| Level | Command | Limit | What |
|---|---|---|---|
| L0 | `ahub wait`, `ahub watch` | 200 B/line | §4 |
| L1 | `ahub status` | 1500 B total | active (phase, pulse, model, round, $), waiting for a decision, open questions, unread; on overflow — counters "N more" |
| L2 | `ahub status T12`, `ahub result T12` | 4000 B | task: goal, state/reason, the worker's result, checks (a diff summary, a test tail ≤ 10 lines), findings without duplicates (≤ 10), cost on one line |
| L3 | `ahub result T12 --full`, `ahub diff T12`, `ahub log T12` | explicit; paged with `--max-bytes` (20000 by default; `ahub log` — 8000) | the full report, the diff, the session log |

All commands: `--json` — the same data machine-readable (no clipping of the text to the limit, the same set of fields).

## 6. Waiting and presence

- `ahub wait [--timeout 30m] [--project P]` — blocks until an event that needs a reaction (respecting the grouping
  window); prints L0 lines of unacknowledged events, marks them delivered; exit code 0. A timeout — empty output,
  exit code 3.
- `ahub watch [--project P]` — for the Monitor: an endless stream of L0 lines; the position is the delivered marks in
  the database (there is no state file: a Monitor restart neither loses nor repeats anything); at the start — one
  summary line about what is delivered but unacknowledged (not the whole tail).
- **Presence**: `wait` and `watch` update `presence(who, project, last_seen, via)` at least once every 60 s.
  Claude "is there" if `last_seen` is younger than 180 s. The observer's escalation and launching Claude from
  Telegram are built on that.
- `who` is `claude` by default (`--who` for other orchestrators).

## 7. Orchestrator commands (CLI, M2)

```
ahub task new --kind scout|code|review|routine --title "goal" (--spec "text" | --spec-file F)
              [--model spark] [--review "spark,mimo-flash" --rounds 2 | --no-review]
              [--paths "core/**,tests/**"] [--accept "tests/test_x.py::test_y"] [--budget 1.5]
              [--after T3] [--resources test_db] [--input <branch|sha|a..b|files>] [--key K] [--draft]
   → "T12 queued (code, spark)" (one line); --key is idempotency (a repeat returns the same task)
ahub status [T12]            L1 / L2
ahub result T12 [--full]     L2 / L3
ahub accept T12 | reject T12 [--reason] | rework T12 --notes "…" | stop T12 | continue T12
ahub wait | watch | ack | inbox | say "text" | ask "question" --options "yes,no" [--task T12] | alarms
ahub models [--role R] | models add … | models role … | models enable|disable <alias>   (registry; it does not
   lift project bans)
```
Exit codes: 0 — success, 2 — refusal (one "error: …" line on stderr), 3 — a waiting timeout.

## 8. Project resources

`.hub.toml`: `max_parallel`, `[resources] name = {capacity, lock}`, `test_resource`. A task declares the resources it
needs (`--resources`); the queue does not start a task while a resource is busy (busyness is the live task processes
with that resource; `lock` is an external flock whose holder is visible in the pulse). Waiting for a slot or a resource
is the phase `waiting` with a reason, not an alarm.

## 9. Default models (the registry at first start)

| alias | provider / model / variant | roles (menu) |
|---|---|---|
| spark | opencode / opencode-go/muse-spark-1.3-contributor / xhigh | executor★, reviewer★, scout★, routine★ |
| spark-high | opencode / opencode-go/muse-spark-1.3-contributor / high | observer★, drafter★ |
| spark-medium | opencode / opencode-go/muse-spark-1.3-contributor / medium | observer |
| mimo-flash | opencode / opencode-go/mimo-v2.6-flash / — | executor, reviewer, routine |
| deepseek-flash | opencode / opencode-go/deepseek-v4.1-flash / high | executor, reviewer, scout |
| spark-free | opencode / opencode/muse-spark-1.3-contributor-free / xhigh | (outside the menu, available explicitly) |
| gemini | agy / gemini-3.8-flash-high / — | (outside the menu — chosen explicitly; window quota) |
| gemini-low | agy / gemini-3.8-flash-low / — | (outside the menu — chosen explicitly; window quota) |
★ — the default in the role.