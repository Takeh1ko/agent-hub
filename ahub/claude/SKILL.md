---
name: ahub
description: Work with agent-hub (ahub CLI) — delegate scout, code, routine or review to cheap worker models (Spark etc.), wait without polling, read the result briefly, accept and merge. Use when a task is cheaper to delegate, or when an ahub event line arrives (DONE/DECISION/ERROR/OWNER/ANSWER/ALARM).
---

# agent-hub: how to work cheaply

The hub runs tasks itself (project copy, worker, gates, review, retries on failures). Your job — file a task,
wake on an event, read the result and decide. Don't poll the hub: it wakes you.

## 1. Session start
- Start a Monitor on `ahub watch` (description: "ahub: events for Claude"). Each line is work for you.
  Monitor lives ≤ 30 min: restart on expiry (events are not lost — position is in the hub DB).
- What's going on: `ahub status` (≤ 1.5 KB).
- Everything is scoped to the repository you work in: a task of another project is refused (run from its repo or
  add `--project X`; `--all` — every project, the owner's view).

## 2. File a task
```
ahub task new --kind scout   --title "goal" --spec "what to find, where to look, what goes in the report"
ahub task new --kind code    --title "goal" --spec-file spec.md --paths "core/**,tests/**" \
              --accept "tests/test_x.py::test_y" [--level 0..4] [--after T3] [--budget 1.5]
ahub task new --kind routine --title "tidy up docs/" --paths "docs/**"
ahub task new --kind review  --title "check the branch" --input "main..feature"
```
- Review level: 0 — none; 1 — docs/routine; 2 — regular code (default for code); 3–4 — near money.
- Spec is self-contained: what to do, which files, how to verify, what not to touch. External facts (API, prices)
  verify with a live call BEFORE filing. A task that fails validation — error comes in one line, no model is called.
- Models: `ahub models` (role menu; project bans cannot be bypassed).

## 3. Woke on an event
- `DONE T12 …` → `ahub status T12` (result, report essence, checks, cost — ≤ 4 KB; confirms the event).
  Full report — `ahub result T12 --full`, diff — `ahub diff T12`, raw logs — `ahub log T12` (only if needed).
- Decide: `ahub accept T12` (scout — accept; code/routine — merge into the working branch, acceptance reruns,
  on red — rollback) · `ahub rework T12 --notes "what to fix"` (same session, new round) · `ahub reject T12`.
- Your own small fix on top of the result: commit in the task copy (`ahub status T12` shows the path), then
  `ahub accept T12` — this is legal ("orchestrator edit"). Need more files — `ahub extend T12 --paths "…"`.
- `DECISION T12 …` → read the reason (`ahub status T12`): rounds over, budget (`ahub budget T12 --add 1` —
  the task resumes itself), files outside allowed paths, failure. Resume — `ahub continue T12`, change model —
  `ahub model T12 mimo-flash`, new spec — `ahub task edit T12 --spec-file …`.
- `ahub nudge T12 "why did you stop? keep going"` — a message into the session of a task that is still working
  (stuck, silent, off-track): the turn is interrupted and the same session continues with your text. Prefer it
  over stop+continue. A queued or finished task — refused in one line.
- `ERROR T12 …` → reason is in the line; usually `ahub continue` after fixing the environment or `ahub reject`.

Old events may start with Russian words ГОТОВО/РЕШЕНИЕ/ОШИБКА/ВЛАДЕЛЕЦ/ОТВЕТ/ТРЕВОГА instead of codes — same events.

## 4. Owner
- `OWNER «…»` → `ahub inbox` (read), reply — `ahub say "short, in the owner's language"`.
- Need approval (merge, extend, spend) — `ahub ask "merge T12?" --options "yes,no"`; the answer comes as
  `ANSWER #N … → yes`. Merge code only with owner approval if they asked for it.
- `ALARM …` → the observer found a problem with the hub itself: `ahub alarms`, `ahub service status`,
  `ahub observer reports`.

## 5. Economy
- Don't read L3 (full report, diff, logs) without need — L2 is usually enough to decide.
- Recon is cheaper than your code reading: give search and survey to the worker, read the essence.
- Parallel tasks — in one batch; dependent — `--after T3` (starts after T3 is accepted, for code with it).
