# agent-hub v2 — architecture (final, abstract level)

Status: final, 2026-09-30 — after two rounds of Spark critique and agreement with the owner.
No technical details: who is in the system, what it does, who calls whom, how a task and its data flow.
Numbers (thresholds, retries, defaults) are orientation from v1; the plan holds the final values.

## 0. Why, and the main principles

agent-hub is a service through which an **orchestrator** (Claude Code or any CLI agent) and a **human** hand work to
cheap worker models, watch that work, and accept the result.

Principles, by importance:
1. **Save the orchestrator's tokens.** The orchestrator receives the minimum needed to decide and gives the minimum back —
   but no less: the work must get done. Details only on explicit request, level by level, with hard size limits.
   The hub wakes the orchestrator when it needs a decision — the orchestrator does not poll.
2. **Working with Claude Code is a first-class case.** A universal interface for any CLI agent is the base; where
   universality hurts quality of work with Claude, Claude wins.
3. **Fault tolerance and observability.** Every error leaves a trace in the logs; every task has an honest pulse;
   a separate observer watches the hub itself; after a crash, restart or update everything continues where it stopped.
4. **Extensible providers.** A model provider is a replaceable module registered by name (today opencode, agy and
   Codex; next a DeepSeek harness…).
5. **The human sees everything and can intervene**, but by default only watches.

## 1. Participants

| Participant | What it is | How it connects to the hub |
|---|---|---|
| Orchestrator | Claude Code (preferred), Codex, DeepSeek harness, any CLI agent | orchestrator handles (§9) |
| Human in a terminal | the owner | terminal app (§10) |
| Human in Telegram | the owner "on holiday" | link to Claude + task view (§11) |
| Worker | a cheap model as executor / reviewer / scout / routine hand | through a provider module (§3) |
| Observer | a system role (Spark, medium/high) | watches the hub (§8) |
| Hub service | one background service supervised by the OS | owns tasks, pulse, observer, Telegram link |

## 2. General scheme

```
  Orchestrator (Claude Code / CLI agent)    Human (terminal)         Human (Telegram)
         │  commands / event stream               │                          │
         ▼                                        ▼                          ▼
 ┌────────────────────── CLIENTS (one set of handles) ─────────────────────────┐
 │ orchestrator handles      terminal app              Telegram bot: link to    │
 │ (CLI · Claude package ·   (view / control)          Claude + task view       │
 │  MCP)                                                              │          │
 └──────────────────────────────────────────────────┬──────────────────────────┘
                                                    ▼
 ┌───────────────────────── HUB SERVICE (core) ────────────────────────────────┐
 │  Model and role registry · Tasks and queue · Engine · Gates · Pulse ·      │
 │  Budgets · Event log and delivery · Logs · Observer · Presence and          │
 │  launching Claude · Task processes (separate, survive a service restart)   │
 └──────────┬──────────────────────────────────────────────┬─────────────────┘
            ▼                                              ▼
 ┌──── PROVIDERS (modules) ────┐                ┌──── PROJECT ARCHIVE ────┐
 │ opencode · agy · codex     │── model sessions ──► │ <project>/.agent-hub/  │
 └────────────────────────────┘   inside the copy  └────────────────────────┘
        ▲ external supervisor (an OS facility): restarts a fallen service
```

- **Every task has one owner.** The steps of a task (preparation, work, gates, review, merge) are performed by its own
  process — while it lives, it is the only one that changes the task's state. The service plans (queue, slots), watches,
  and takes the task over only when its process is dead. Clients (CLI, terminal, Telegram, MCP) never change state
  directly — they ask the service or the task owner through handles. Transitions are idempotent: a repeated command
  or a restart in the middle of a step never duplicates work (no double merge, no second session).
- One hub per machine, serving all projects; the project is determined by the directory a command came from.
- Hub state lives in its own storage (outside projects). Inside a project there is only a local archive for the human
  (§12).
- **The truth about work lives at the sources**: the provider (activity, tokens, money), git (commits, diff), the OS
  (are processes alive). The hub's storage holds relations, queue, events, decisions; anything disputed is rechecked
  against the source.

## 3. Model providers

A provider is a module that works with one source of models the user has access to. All modules answer one
**capability contract**; the core does not know how a concrete provider is built.

The contract:
- **Catalog:** models, reasoning variants, prices or quotas, what a model can do.
- **Session:** start with an assignment in a given directory; continue the same session with a new message (the session
  id is caught at start; there is a fallback way to find the session if it was not caught); interrupt; whether the
  process and its children are alive.
- **Activity stream:** steps, tool calls (which one is running and for how long), text, errors — normalized, plus
  raw events for analysis.
- **Outcome:** the last answer; a structured result in a given shape if the provider can do that; completion status —
  **classified**: success / model error / network-server failure / silence / quota / no access.
- **Accounting:** tokens, money by counters (a Go subscription "at plan price", real money, quota windows), limit left.
- **Session log:** a full transcript on request (deep analysis, not for the orchestrator).
- **Health:** reachable, authorized, network/proxy working, quota not exhausted.
- **Isolation:** work only in the given directory, without access to the live hub storage or to secrets.

Division of responsibility: the provider **classifies** the failure, the core **decides** what to do (retry, wait,
"needs decision"). Every module declares which parts of the contract it supports; what it does not have is an honest
"no data", not a failure. A new provider is a new module; the core does not change.

A model in the hub = provider + model + reasoning variant under a short name (`spark`, `spark-medium`, `mimo-flash`…).

## 4. Model and role registry

Roles: **executor**, **reviewer**, **scout**, **routine hand**, **observer** (system), **drafter** (system).

- Every role has a set of allowed models (add/remove) and a default model.
  Defaults: executor, reviewer — Spark 1.3, MiMo 2.6 Flash, DeepSeek v4.1 Flash; scout — Spark 1.3, DeepSeek;
  observer — Spark 1.3 medium/high; drafter — Spark 1.3 high.
- A set is a **menu**: the default model is used, or an explicitly chosen one. **No automatic substitution**: if a model
  does not work, that is visible (pulse, event), and a human or Claude changes the task's model by hand with the
  "change model" handle.
- The project level narrows the menu (a project can ban DeepSeek; at hub level it is available).
- A human changes the hub menu in the terminal (control mode) or the orchestrator changes it with a handle.
  **A project ban is lifted only by a human** — the orchestrator handle does not bypass it.

## 5. Tasks

### Kinds and gates
| Kind | Worker | Result | Files | Gates |
|---|---|---|---|---|
| **Scout** | studies code / documents / information | report | changes nothing | the report exists and fits the shape; no changes |
| **Code** | writes code and tests in its own copy of the project | branch + report | through a merge | strict: a commit exists, the diff ⊆ allowed files, acceptance is green under the project lock, the worker's report fits the shape |
| **Review** | checks an explicitly given input: a branch, a commit, a range or files | findings | changes nothing | findings in the given shape (file, line, what to fix) |
| **Routine** | light file work (documentation, laying out files, renames; "documents" belongs here too) | changes + report | through a merge | light: a commit exists, the diff ⊆ allowed files, no acceptance tests |

A minor gate failure (no commit, no report in the shape) → one "fix it and report back" in the same session; a second
one → Error.

### What a task is given at creation
- a goal (one sentence) and a description (text or a file);
- the kind and the shape of the result (free report / template / changes);
- the executor model (by default — from the role);
- review: none / N rounds / which models;
- limits: allowed files, the acceptance criterion, budget, time limit, "after task X", "not in parallel with …".

Everything except the goal and the description has per-kind defaults. Before it starts, a task is checked for
completeness and contradictions (the files exist and the project allows them, acceptance collects, the model is
available) — if it does not pass → refusal with a reason, no model is called with money.

**Task draft.** A human (terminal) writes the goal and description in their own words → a model fills in the rest
(kind, files, acceptance, models) → preview → the human edits if they want → an explicit "Start". Without an explicit
start the task does not run. The orchestrator files tasks fully from the start (it knows the fields), or also through
a draft.

### Lifecycle (in words)
```
Draft → Queued → Preparing → Studying → Writing → Checking → Reviewing → Fixing → … →
   → Done  |  Needs decision  |  Error  |  Stopped
   → Accepted (merged / report accepted)  |  Rejected
```
- Scout and review: Preparing → Studying → Reporting → Done.
- The phases "Studying / Writing / Checking / Reviewing" are derived from worker activity and the engine stage.
- "Done" → the decision belongs to the orchestrator or the human: accept / send back for rework with findings /
  reject. The hub never merges on its own.
- **An "after X" dependency** is met only when X is **accepted**; the new task starts from the code that already
  contains X. If X is rejected or in Error, the dependent tasks move to "Needs decision" with the reason.

### Active tasks and history
- **Active:** phase, pulse, model, round, spend, how long it has been going.
- **History:** outcome, cost, duration, review rounds, models, decisions, reports, diff — briefly in the list, in
  detail on request.

## 6. The task engine (how everything is kicked)

A code task:
1. **Intake and validation** (§5) → **Queue**: a free slot (the project's parallelism limit), "after X", the resources
   the task names (a shared test database, an external service that is "strictly one at a time"). The project test
   resource is not among them: acceptance takes its lock by itself (§5, gates), so code tasks are not serialized by it.
2. **Preparation:** a separate copy of the project (without secrets) and a branch; the environment (tests collect,
   the project hooks have run, the lock is available). Preparation fails → Error with the reason, the model is not
   called.
3. **Work:** the provider starts a session with the assignment (rules + task). Activity → pulse and phases.
   A classified failure → a core decision: network/server failure → retry after a pause (the same session, if it is
   known); silence → interrupt and continue **once** in the same session, then → Needs decision; quota → wait for the
   window or Needs decision; no access / model error → Error.
4. **Gates** (§5, no models involved).
5. **Review:** reviewers in new sessions see the task and the changes, but not the orchestrator's reference decisions.
   All agree → Done. Findings → Fixing in the same executor session (a new round). A dispute counts only with a
   justification and a place in the code. Rounds are over → Needs decision.
6. **Outcome:** a "Done / Needs decision / Error" event → delivery to the orchestrator (§9) → a record in the archive
   (§12).
7. **Acceptance:** accept (merge into the working branch → acceptance once more → roll back if it fails) / rework /
   reject. Legal paths for the orchestrator: **its own fix on top of the result** ("orchestrator edit" — recorded in
   the log, then the usual gates) and **widening the allowed files** by an orchestrator decision (an explicit list,
   recorded in the log). The diff for review, for the gates and for the merge are all counted from the same base.
8. **Continue** (after Stopped / Error / Needs decision): uncommitted work is kept, the task continues in the same
   session; if the assignment has changed (a different text fingerprint) — in a new session.

Scout: steps 1–3, "the report exists and fits the shape" → Done. The orchestrator reads the report (compressed to
what it needs) and closes the task.

**Budget** (configurable, by Go counters / real money / quota) is counted for the **whole task**, review included.
At 80 % — a note in the log (not sent to the orchestrator); at 100 % — no new steps, the worker gets
"save your work and stop" (cooperatively, not killed in the middle of a step), the task → "Needs decision".
"Extend by N" is one action: the budget is raised, the task continues, an event goes into the log; "no" → Stopped.
Waiting for a quota window is "waiting for a reason", not an alarm.

**Queue and resources:** a project declares a parallelism limit and shared resources (a test database, an external
service that is "strictly one at a time"). Busyness is counted by the task processes actually running. Waiting for a
slot or a resource is "waiting for a reason" with a cause, not an observer alarm. An "after X" task starts from the
working branch that already contains X.
The **test resource is not held by the queue**: a code task does not get it added to its resources, because acceptance
takes the same flock only while the tests run (§5, gates) — code tasks go in parallel and their acceptance runs wait
for the lock one after another (the phase "waiting for the test lock — held by T<n>", the holder is visible in the
pulse). A task that needs exclusivity for its whole run names the resource itself (`--resources`).

## 7. Pulse

The pulse is **proof of life**, collected from several sources: the provider's activity stream, processes (the agent
itself and its children: tests, waiting for the lock — and who holds the lock), the active tool, the freshness of the
provider's data.
- 🟢 **Working** — fresh activity.
- 🟡 **Waiting for a reason** — no activity, but there is an explanation: a long tool, tests, a lock, a quota window.
- 🔴 **Silent** — neither activity nor an explanation, longer than the threshold of the phase (each of work, review and
  tests has its own threshold).
- ⚫ **Dead** — there is no process, and the task is not in a final state.
- ⚪ **No data** — the provider does not give the signal needed (honestly, without guesses).

The hub service counts the pulse continuously. "Silent" → an engine action (§6.3). "Dead" (orphaned) → after N minutes
with no process and no session the task returns to the queue and continues where it stopped, with an event in the log;
a repeated orphaning → Needs decision.

## 8. Observer

A system role that watches **the hub, not the projects**.

1. **Every 5 minutes — code, without a model:** the pulse of all active tasks, WARNING/ERROR in the hub logs for the
   period, provider health (network, proxy, authorization, quota) and component health (queue, task processes, the
   Telegram bot, event delivery). All clean → nothing. Suspicion → an observer model looks into it: a false alarm →
   the log; a real one → escalation. The same problem is not looked into again while it has not changed (a pause).
2. **Every 30 minutes — a model regardless, by checklist:** are there tasks that are "working" but have had no result
   for a long time; is the queue growing while slots are free; are there orphans; do the costs match the activity; is
   the Telegram bot and the event delivery alive; are the logs quiet while there are obvious problems; is everything
   all right with the providers.
3. **Escalation:** an ordinary alarm → Claude; no reaction for 15 minutes → the human in Telegram. **Critical** (the hub
   cannot work: a provider is down, the service has degraded) → immediately both to Claude and to the human.
4. **Who watches the observer:** the hub service knows the time of its last check and raises an alarm when it is
   missed; the OS facility backs the service itself (restart on a crash). The observer's reports are kept.

## 9. Orchestrator handles

### Projects and scope
One hub, one database, several repositories. A Claude session works in one repository and must see and touch only
that project; the owner — the human, the terminal app, Telegram — sees everything. The scope of a handle comes
from one place: `--all` — every project (the owner's mode), `--project X` — X, otherwise the project of the
current directory (`.hub.toml` is searched upward); outside every project — every project. The MCP server
resolves its scope once, from its own working directory, when it starts; a tool call may name another project.

Everything an orchestrator reads carries its project: tasks, events, questions (the answer too), the human's
messages. A row with an empty project — the observer's alarms, the service's own events — is hub-wide and belongs
to every scope. Acknowledgement follows the scope: reading the inbox or acknowledging events in one repository
never marks another repository's events or messages read. Commands that name one task belong to that task's
project: a task of another project is refused, with the way out — run from that repository or add
`--project X` — unless `--all` or a matching `--project` was given. Per-project money counts the hub's own
sessions; per-project budget caps and model menus are deliberately not part of this.

### Event delivery (without losses)
- Every event that needs a reaction (Done, Needs decision, Error, a message/an answer from the human, an alarm) is
  stored with "delivered" and "acknowledged" marks. The orchestrator **explicitly acknowledges** that it took the
  event; an unacknowledged one does not disappear. A restart of the wakeup stream (the Monitor lives ≤ 30 minutes), a
  hub restart, a closed session — the events will wait.
- The **wakeup stream** yields only new unacknowledged events: critical ones at once, the rest batched over a window;
  it remembers where it stopped; on the first start it does not dump everything old but gives one summary.
- **"Wake me"** — blocking wait until new events or a timeout; returns only the delta.
- **Claude presence** — a fresh mark of its wait/stream, **per project**: `presence_project`, one row per
  (who, project), so a session in one repository does not look like a session in another. The owner's stream (every
  project) stamps a row for each of them. No mark longer than the threshold for a project = no Claude there. The launch
  from Telegram (§11) asks the question per project — one launch for A must not wait for the Claude of B; the
  observer's escalation (§8) asks it for the whole hub, since it watches the hub. The pre-migration table
  `presence` (one row per `who`) is still written and read as a fallback, so a process on the previous code (a live
  reload) neither breaks nor looks absent.

### How tokens are saved
- **Detail levels with a hard size limit:** L0 — one line ("is there anything for me"), L1 — a summary
  (≈ up to 1.5 KB for everything), L2 — task details (findings without duplicates, phases, spend), L3 — raw material
  (diff, session log) — only on explicit request.
- **A result ready to decide on:** scout — a report compressed to the essence; code — what was done, a diff summary,
  the outcome of the checks with the tail of the test output, review findings without duplicates, one line of cost.
- Budgets, pulse, phases — not sent until they become a problem.

### What the handles can do
- tasks: create (at once or through a draft), list active, status, result, accept/merge, send back for rework, reject,
  stop, continue, change model, extend the budget;
- history: list, in detail per task;
- model registry: look, change;
- link with the human: write to Telegram, ask a question with options (for example "merge T12?"), get an answer;
- observer alarms: look, mark as looked into;
- waiting: "wake me when there is something for me".

### Three forms of the same handles (in the order of implementation)
1. **Command line** with compact output — understandable to any CLI agent.
2. **Package for Claude Code** — a skill (how to work with the hub cheaply), an event stream for the Monitor, project
   setup with one command.
3. **MCP server** — the same handles as tools (for Codex and others); it is in the architecture, implemented after 1–2.

## 10. Terminal app (the human)

- Screen: active tasks (phase, pulse, model, spend), history, task details, the event log, the model registry, observer
  alarms, provider health.
- **A "View / Control" toggle.** In view mode the action buttons are hidden. In control mode: create a task (through
  a draft, §5), stop, accept/reject, change model, extend the budget, change the registry.
- All in words, understandable to a non-programmer.

## 11. Telegram: a link to Claude + a view

Telegram is **not a remote control for the hub**: tasks are not created or changed from there directly. It is a link to
Claude, like a senior in the office while the owner is on holiday, plus a convenient view.

**Which project a message belongs to:** the prefix in the text (`по agent-hub: …` / `agent-hub: …`), else the project
the chat picked last (`/project`, or the prefix of an earlier message; a pick that is no longer in the hub is dropped
with a notice, so the message is not stranded), else the whole hub. A hub-wide message is in every project's inbox,
and the launcher serves it to the owner alone — it is never mixed into a project's prompt.

**Link to Claude:**
- A human's message → an event for the orchestrator of that project:
  - a live session exists **in that project** (presence §9) → the message goes into it; that session reads its own
    inbox (`ahub inbox`) and no second one is started;
  - no live session → **the hub launches Claude Code** (`--dangerously-skip-permissions`, as the owner normally runs
    it) **in that project's own directory, with that project's messages only** and an L1 summary of that project; the
    prompt tells it which scope it has (`--all` for a hub-wide launch). It passes on: the skill for working with the
    hub, the summary, the messages. That Claude controls the hub through the handles and answers.
- **One launched Claude per project.** The pending messages are grouped by project, and each group without a live
  session (and without a Claude the hub has already started for it) gets its own — a live session in A never holds back
  a launch for B, and the owner (a hub-wide message) gets one of his own. While a launched Claude works, the new
  messages of that project queue: it picks them up before finishing, the rest goes to the next launch.
- A launched Claude is a supervised process (timeout, pulse, the launch journal) and its session is kept: **one
  continuable "Telegram session" per project** remembers the conversation — a new one when the old has grown too large
  or a day has passed. Launches are limited per hour (a limit of the whole hub; one launch spends it) and go into the
  log.
- **A group with no directory cannot be started** — a project that is not in the hub config (and the hub-wide group
  when the hub has no projects at all). That is reported **once**, not on every tick: the bot tells the owner, the log
  keeps a warning, and the report is due again only after a pause.
- Claude → human: answers, questions with buttons (for example, approving a merge), reports on request.
  **A button is an answer addressed to Claude** — he acts on it through the handles; the hub itself does nothing on a
  button.

**Task view (read only):** a list of active tasks with buttons → pressed — task details (what we are doing, phase,
model, pulse, spend, findings) in words; recent tasks the same way.

**The hub writes to the human directly** only for observer alarms (§8.3). Task outcomes reach the human when Claude
decides it is worth telling.

## 12. Project archive

In the root of the working project — a local folder for the hub (not in the project's git), for the human. The hub only
writes; a human cleans it.
- per task: the assignment, worker reports, the final diff, review findings per round, orchestrator decisions, cost and
  duration, the outcome;
- a common list of the project's tasks (what, when, how it ended, what it cost).

Heavy session transcripts live in the hub's storage; the archive holds a link or a digest.

## 13. Fault tolerance and logs

- The logs of all components are structured, with levels; every error and warning has a task, a component and a reason;
  rotation. The task event log is separate from the logs.
- **Task processes are separate from the service:** a service restart does not kill them; after starting, the service
  finds the running tasks by OS processes and keeps leading them. A slot is busy if any task of the project is really
  running, not only "its own".
- **Updating the hub's code:** the service restarts itself onto the new code when it sees a change, without touching
  running tasks; new tasks start on the new code.
- Provider failures are told apart (§3) and handled differently (§6.3).
- Workers are isolated: their own environment (without the hub's tokens; and in hooks), a copy of the project without
  secrets, no access to the live hub storage. opencode itself refuses access outside the given directory. **agy does
  not**: in headless mode it denies every command unless it is started with `--dangerously-skip-permissions` (checked
  live 2026-10-03), and no agy mode limits writing to a directory — `accept-edits` writes files without questions but
  forbids commands, which makes the turn empty. What holds agy in place is the copy itself (cwd), the clean
  environment and the gates (the diff ⊆ the allowed files, counted from the base before a merge). The limit: without
  an OS sandbox (a separate user/namespace) a process of the same user can technically read and write files outside
  the copy — an accepted risk (the models are ours, the tasks come from Claude); hardening it is a separate task if
  needed.
- **Codex has a real OS sandbox** — the first provider that does: `-s workspace-write` reads anything but writes only
  the working copy, enforced by the kernel (Landlock inside bubblewrap on Linux, Seatbelt on macOS), so the worker can
  run git and pytest in the copy and cannot touch anything outside it. The flip side must be checked on the host:
  `codex sandbox <mode> -- true` starts the sandbox without a model and without the network, and if it fails codex
  fails every command *silently* (the JSONL stream shows nothing — the error goes only to the model), so the turn comes
  out empty. `check_codex`/`health()` therefore run this probe and report it as a problem (checked live
  2026-10-03: inside a container without user namespaces bubblewrap cannot set a uid map —
  `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`; on a normal host the probe passes). The mode is the
  admin's choice in the hub config — `[providers.codex] sandbox` (`read-only` | `workspace-write`, the default |
  `danger-full-access`), passed to `exec` as `-s` and to `exec resume` as `-c sandbox_mode=…` (that one has no `-s`).
  On Ubuntu 24.04 the probe fails out of the box: AppArmor restricts unprivileged user namespaces
  (`kernel.apparmor_restrict_unprivileged_userns = 1`), so bubblewrap cannot start and every command of the turn fails
  silently — the turn looks empty. `ahub doctor` detects exactly this case and gives both fixes in one hint: allow the
  namespaces (`sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0`, kept by a file in `/etc/sysctl.d/` —
  the admin's decision) or `sandbox = "danger-full-access"`, where the task copy and the gates hold codex as they hold
  opencode and agy; the hub never changes system settings by itself. A mode without an OS sandbox is not probed (there
  is nothing to start) and so cannot be reported as a failure. Non-interactive
  mode is `-c approval_policy="never"` (the worker never waits for a human) plus `stdin=/dev/null`, which the shared
  runner already gives the process; `--skip-git-repo-check` is added when the working copy is not a git repo
  (codex otherwise stops to ask about the trust — on `exec resume` too).

## 14. v1 lessons (mandatory requirements — a checklist)

From the field issues of 2026-09-30:
1. A dependency only from an accepted task (§5).
2. Updating the code without the queue stalling (§13).
3. The parallelism limit counts all really running tasks (§13).
4. An answer to "extend the budget?" is really applied (§6).
5. An orchestrator fix after review is a legal path to a merge (§6.7).
6. An orchestrator decision can widen the allowed files (§6.7).
7. Orphaned tasks do not hang (§7).
8. Continuing on a "dirty" copy does not break (§6.8).
9. The silence of a paid model is one auto-continuation, not an instant failure (§6.3).
10. Waking Claude loses no events on a Monitor restart (§9).
11. The review diff, the gates and the merge are counted from the same base (§6.7).

## 15. Owner decisions (recorded)

1. The hub service is one background process; the CLI, the terminal app and Telegram are clients.
2. Handles: CLI → Claude Code package → MCP (later).
3. Claude Code is launched by the hub only if the live session of that project has died; one launched Claude per
   project (a hub-wide message — one of the owner's own); `--dangerously-skip-permissions`; the Telegram session is
   continuable per project.
4. Observer: 5 min code, 30 min model (Spark medium/high); escalation: ordinary — after 15 min without a reaction,
   critical — to both at once.
5. Model menus without automatic substitution; a model is changed by hand (a human or Claude).
6. A human files tasks in the terminal through a draft (text → a model fills it in → preview → start).
7. Telegram: a link to Claude + a task view with buttons; not a remote control.
8. Task events go to Claude; Claude writes to the human. "Document" ⊂ "Routine". One hub per machine. The archive is
   write-only.
9. A project-level model ban (e.g. DeepSeek banned in one project); only a human lifts it.
10. A message from Telegram belongs to the project it names (prefix, or the last `/project` of that chat); an unnamed
    one is the hub's — such a Claude starts where it worked last, else in the first project.