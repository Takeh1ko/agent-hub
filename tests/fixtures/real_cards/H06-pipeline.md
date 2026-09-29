# H06 — конвейер: запуск агентов, круги, ревью, слияние (`hub start/continue/review/merge/stop/clean`)

**Цель.** agent-hub сам ведёт задачу от карточки до слияния, используя ворота H02/H03 и учёт H01/H04. После H06
PlayerUP запускает задачи через `hub start`, а `PlayerUP/tools/agents/run_task.py` **не удаляется в v1** (замораживается).

**Прочитать.** docs/spec.md §2, §6–§8, §11; docs/spec.critique_r2.md (Н5, Н7, Н8, Н10, Н11 — обязательно);
карточки H01–H04 (интерфейсы); образец: /home/takehiko/Projects/Python/PlayerUP/tools/agents/run_task.py —
`OpencodeRunner`/`AgyRunner` (как ловится `sessionID`/conversation_id, `prompt_arg` для длинных промптов, таймауты),
`PanelReviewer` (параллель, per-reviewer файлы, неответивший = low, никто = arbiter), `run_cycle`, `build_*_prompt`,
`_diff` (лимит, исключения фикстур), `main` (`--continue-work`: база = merge-base, старое `.agent` → `.agent.prev_<ts>`).

**Можно менять.** `hub/pipeline/**` (новый), `hub/commands/start.py`, `hub/commands/continue_.py`, `hub/commands/review.py`, `hub/commands/merge.py`,
`hub/commands/stop.py`, `hub/commands/clean.py`, `hub/commands/queue.py` (новые),
`hub/migrations/004_pipeline.sql` (новый, если нужно), `tests/test_pipeline*.py`, `.hub.toml`,
`docs/examples/PlayerUP.hub.toml`, `docs/spec.md` (§7–§8, §11 — уточнения по факту).

**Интерфейс.**
1. `hub/pipeline/runners.py`: `Runner` (протокол: `start(prompt, cwd, log) -> session_id`, `resume(session_id, prompt,
   cwd, log) -> session_id`); `OpencodeRunner(model, variant, timeout_s)`, `AgyRunner(timeout_s)`; модели — таблица
   `MODELS` (`muse`, `musefree`, `mimoflash`, `mimo`, `mimofree`, `deepseek`, `glm`, `gemini`) как в run_task.
   Каждый старт → `store.link_session(external_id, tool, task_id, role, round, model)` **сразу**, как только id известен
   (не в конце) — иначе пульс/roster не видят сессию.
2. `hub/pipeline/prompts.py`: `executor_prompt(rules, card) `; `fix_prompt(findings, gate)`; `review_prompt(rules,
   card, diff, gate, round, blind)` — при `blind` карточка через `strip_arbiter` (H02); в конце промпта исполнителя —
   буквальный шаблон `.agent/done.json` (≤ 15 строк) и правило «без коммита и done.json работа не принята».
3. `hub/pipeline/cycle.py`: `run_task(store, project, task_id, runners, rounds=2) -> str` (итоговый этап):
   `preflight` (H02) → exec r1 → `check_gate` (H03; `allowed` = «Можно менять» ∩ `allowed_paths` проекта, выход за
   `allowed_paths` = `failed`, за карточку = changes) → нет коммита/done.json → **repair** (`REPAIR_PROMPT` H03 в ту же
   сессию, один раз; повтор провала → `failed`) → панель ревью параллельно (новые сессии) → `verdict` (H03) →
   changes: `fix_prompt` в сессию исполнителя → следующий круг … Этап пишется в store на **каждом** переходе
   (`stage`, `round`, `stage_reason`) + событие `stage` — это читают wait/бот/TUI.
4. Бюджет: перед каждым шагом модели — `cost` задачи из opencode `session` (агрегаты `cost`, `tokens_*` — Н3) против
   `task.budget_go`/`budget_usd`; 100 % → кооперативная остановка (repair «закоммить и остановись»), этап `stopped`,
   `hub ask` «продлить на $X?». Лимит Go в v1 — один котёл на модель $60/мес (упрощение Н5 записать в spec).
5. Очередь и идемпотентность: `hub start CARD [--executor] [--reviewers] [--rounds] [--budget-go] [--after ID]
   [--blind]` → `lint` (H02; не OK → отказ с причинами) → задача `queued` (повтор с тем же `card_hash`+`base_sha` —
   тот же task, не новая). Воркер очереди — `hub queue run [--max-parallel 4]` (фоновой процесс; `nohup`), берёт
   `queued` по `created_at`, уважает `meta.queue_paused` и `--after`; задача с `Сеть: playerok` — не параллельно
   с другой такой же.
6. `hub continue ID` (новые «Решения арбитра» в карточке → та же ветка, база = merge-base, `.agent` → `.agent.prev_<ts>`),
   `hub review ID [--blind]` (только ворота + ревью), `hub stop ID` (кооперативно; жёстко `--kill`),
   `hub merge ID` — только из `ready` (или `--force` из arbiter): перепроверка ворот по git, `merge --no-ff` в
   `work_branch`, приёмка, откат при красных, `push` по конфигу, хук `task_cleanup`, удаление worktree/ветки;
   конфликт → `merge --abort`, этап остаётся, событие. `hub clean` — осиротевшие worktree/ветки из
   `git worktree list --porcelain` без задачи в store (по умолчанию — только показать, `--yes` — удалить).
7. События владельцу: `owner_command` (H05: /stop, /merge) исполняются воркером очереди.

**Приёмка.**
- `pytest -q tests/`
- Фейковые runners (без сети): полный цикл ready за 1 круг; changes → 2-й круг → ready; нет коммита → repair → ok;
  repair дважды провален → failed; dispute без file:line → считается changes; никто из панели не ответил → arbiter;
  выход за `allowed_paths` → failed; бюджет 100 % → stopped + вопрос; повторный `start` той же карточки → тот же task.
- Временный git-репозиторий: `merge` из ready сливает и чистит; конфликт → abort, этап не меняется; красные тесты
  после слияния → откат.
- `session` связывается со store до завершения шага (тест: во время фейкового шага `snapshot.build` видит сессию).

**Нельзя.** Трогать PlayerUP (в том числе удалять `run_task.py`); реальные модели/сеть в тестах; `git add -A`;
блокировать store дольше транзакции.

**Сеть.** нет. **Уровень.** hard. **Исполнитель.** muse.

**Коммит.** `feat: конвейер agent-hub — очередь, runners, ворота, repair, панель ревью, merge/stop/clean`

## Решения арбитра (круг 3)

«Можно менять» исправлен (ворота не понимали `{a,b}`) — scope-нарушения по `hub/commands/*.py` сняты.
Принять high/medium ревью круга 2 (`.agent.prev_*/review_r2.json`), каждое — с тестом:
1. **HIGH.** Панель: каждому ревьюеру свой файл `review_rN_<имя>.json` (подмена в промпте, как `PanelReviewer`).
2. **HIGH.** `owner_command`: id задачи берётся из `event.task_id` (так пишет бот H05); `/stop` `/merge` реально исполняются.
3. **MEDIUM.** `hub queue run` — долгоживущий цикл (опрос очереди и owner_command раз в 10 с, выход по SIGTERM/`--once`).
4. **MEDIUM.** `fix_prompt` при пустых ошибках ворот пишет «ворота: зелёные».
5. **MEDIUM.** `hub clean` снимает префиксы `*`/`+` у веток (ветки в worktree) — осиротевшие реально удаляются.
6. **MEDIUM.** `budget_usd == 0` — запрет трат реальных денег (стоп при usd > 0); `budget_go == 0` — без лимита;
   `--budget-usd` и `defaults.budget_usd`.
7. **MEDIUM.** Повторный `hub start` той же карточки при другом base_sha не затирает задачу в финальном этапе
   (merged/ready/arbiter) — новая задача с суффиксом или отказ с причиной.
