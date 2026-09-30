# agent-hub — архитектура (шпаргалка для Claude)

Сжатая карта кода, чтобы не изучать проект заново. Подробная спецификация — `docs/spec.md`,
открытые проблемы — `docs/field_issues_2026-09-30.md`, бэклог — `docs/tasks/_backlog.md`,
заметки о моделях — `docs/model_log.md`. Сверено с кодом 2026-09-30 (commit 8dc9d32).

## Что это
Терминальный оркестратор ИИ-агентов-кодеров. Claude (архитектор) пишет **карточку** задачи →
hub сам создаёт git worktree, запускает дешёвую модель-исполнителя (opencode: Spark/MiMo; agy: Gemini),
проверяет результат **воротами** (коммит, дифф ⊆ разрешённых файлов, приёмочные тесты, `done.json`),
гоняет **ревьюеров** в чистых сессиях, крутит круги доработки и доводит до `ready` → `hub merge`.
Владелец (не программист) видит всё в TG-боте и в `hub top` (TUI), может ставить задачи текстом.
Проекты: сам agent-hub и PlayerUP (`~/.config/agent-hub/config.toml` → список путей к репо с `.hub.toml`).

## Жизненный цикл задачи
```
карточка docs/tasks/<ID>-*.md
  └ hub lint (gate/lint.py)            — разделы, globs ⊆ allowed_paths, пути, pytest --collect-only
  └ hub start (commands/start.py)      — task в hub.db, ветка agent/<ID>, worktree в <worktrees>/<ID>, stage=queued
  └ hub queue run (commands/queue.py)  — воркер: берёт queued, уважает --after/паузу/лимит/«Сеть: playerok»,
                                         каждую задачу — отдельный процесс `python -m hub.commands.queue --run-one ID`
      └ pipeline/cycle.py: run_task()  — ЕДИНСТВЕННЫЙ писатель этапа
          preflight (gate/preflight.py: чистый worktree, rules, хук task_setup, collect, замок)
          for round 1..N:
            exec rN   — executor.start/resume (runners.py) с prompts.executor_prompt / fix_prompt
            gate rN   — done.json (gate/donefile.py) + check_gate (gate/gate.py); провал → 1 repair-промпт
                        в ту же сессию (gate/repair.py), второй провал → failed
            review rN — ревьюеры в новых сессиях (prompts.review_prompt, «Решения арбитра» вырезаны)
                        → .agent/review_rN_<model>.json → gate/verdict.py
            все approve → ready; changes → следующий круг; круги кончились → arbiter
          бюджет превышен → stopped + вопрос владельцу «продлить?»
  └ hub merge (pipeline/merge.py)      — только ready (--force из arbiter): --no-ff в work_branch,
                                         приёмка под замком, откат при красном, push по конфигу, cleanup
```
Этапы: `queued → preflight → exec rN → gate rN → review rN → … → ready | arbiter | failed | stopped → merged | dropped`.

Устойчивость в cycle.py: `TransientError` (сбой сети/сервера opencode) → повтор до `retry_max` с паузой;
сторож тишины (`idle_s`, 900 с) → musefree переключается на muse один раз; `hub continue` — wip-коммит +
продолжение той же сессии (новая, если sha карточки изменился); `hub stop` — файл `.agent/stop_requested`.

## Карта модулей (`hub/`, ~15 тыс. строк)
| Модуль | Роль |
|---|---|
| `cli.py` | argparse; автообнаружение `hub/commands/*.py` с `register(subparsers)` |
| `commands/` | по файлу на подкоманду: start, queue, continue_, review, merge, stop, clean, status, wait, findings, ask, say, inbox, cost, roster, lint, preflight, gate, new, top, bot, import_legacy |
| `config.py` | `ProjectConfig` из `.hub.toml` (+ idle/retry), `load_projects()` из глобального конфига |
| `store.py` | `Store` — `~/.local/share/agent-hub/hub.db` (или `$AGENT_HUB_HOME/hub.db`), SQLite WAL, миграции `hub/migrations/*.sql` |
| `time.py` | UTC ms в БД, Asia/Yekaterinburg на экране, парсинг «сегодня 20:00», «2ч» |
| `secrets.py`, `tg_send.py` | токен бота; простая отправка в TG из скриптов |
| `read/` | **единый слой чтения** (CLI, TUI, бот): `opencode.py` (opencode.db mode=ro: сессии, $, пульс, активный tool), `agy.py`, `procs.py` (/proc), `git.py`, `events.py`, `findings.py`, `human.py` (словарь «по-человечески» для top/TG), `snapshot.py` (`build()` → `Snapshot`, `to_text` ≤ 1,5 КБ) |
| `gate/` | чистые функции: `lint.py` (+`strip_arbiter`), `preflight.py`, `gate.py` (`check_gate`, `effective_base`), `donefile.py`, `acceptance.py` (pytest-ноды из «Приёмки»), `repair.py`, `verdict.py` |
| `pipeline/` | `runners.py` (OpencodeRunner/AgyRunner, `MODELS`, TransientError, сторож тишины), `cycle.py` (`run_task`), `prompts.py`, `review_levels.py` (раздел «Ревью» 1–4), `merge.py` (+`list_orphans`), `draft.py` (задачи владельца → карточка моделью), `common.py` (meta, card globs, base) |
| `bot/` | `core.py` — чистая логика (форматирование, группировка событий 5 мин, вопросы, подтверждения, snapshot→события); `run.py` — aiogram 3, long polling через HTTPS_PROXY, циклы outbox/poll, sync-обёртки через `to_thread` |
| `tui/` | `app.py`, `widgets.py` — `hub top` на textual, опрос 2 с |

`tools/` — скрипты вне пакета: `claude_watch.py` (поток событий для Monitor Claude: ГОТОВО/АРБИТР/ОШИБКА…),
`observer.sh` (наблюдатель-модель раз в N мин), `routine.sh` (разовый вызов Spark), `market_digest.sh`,
`night.py` (устаревший ночной диспетчер до H06), `pulse.py`, `roster_loop.sh`, `after*.sh`, `continue.sh`.

## Данные
**Источники истины — чужие:** `opencode.db` (токены/$/пульс), git (коммиты/дифф), `/proc` (жив ли процесс).
Чужие SQLite — только `file:…?mode=ro`, короткие соединения.

`hub.db` (связи и очередь): `task` (id, project, card_path, card_hash, level, branch, worktree, base_sha, stage, round,
executor, reviewers_json, stage_reason, budget_go/usd, merged_sha, blind…), `session` (external_id ↔ task, role, round,
model), `event` (kind: stage/stuck/crashed/budget_*/owner_message/answer/question; флаги seen_claude/sent_tg),
`question`, `inbox`, `outbox` (сообщения владельцу от `hub say`), `budget`, `meta` (queue_paused, queue_stop,
claude_listen_ts, tg_*), `tg_chat`, `draft`.

Почтовый ящик агента в worktree `.agent/`: `done.json` (`{commit, files, tests{cmd,ok,tail}, notes}`),
`review_rN_<model>.json`, логи `executor_rN.log` / `reviewer_rN_<model>.log` / `repair_rN.log`, `stop_requested`,
`hubhome/` (изолированная hub.db агента: `AGENT_HUB_HOME`).

## Модели и конфиг
`runners.MODELS`: `muse` = opencode-go Spark 1.3 xhigh (основной), `musefree` (бесплатный Spark),
`mimoflash`, `mimo`, `mimofree`, `glm`, `deepseek` (не использовать — дороже), `gemini` (agy, выключен).
`.hub.toml`: root, worktrees, rules, python, test_lock, work_branch, push, allowed_paths, `[hooks]`,
`[defaults]` executor/reviewers/budget_go (1.5)/budget_usd (0), `[levels]` easy/medium/hard/background → модель,
`[draft] model`. Раздел карточки «Ревью: 1–4» задаёт состав панели и число кругов.

## Процессы в работе
- `hub queue run --project P` (nohup) — воркер очереди на проект, дети `--run-one`.
- `hub bot` — единственный долгоживущий демон: TG + раз в 15 с Snapshot → события/уведомления, сводка раз в 60 мин.
- Claude: `hub status`, `hub wait` (фоном), Monitor на `tools/claude_watch.py`; отвечает владельцу `hub say`.
- Владелец: TG (`/status /roster /task /stop /merge /budget /pause /new`, кнопки на вопросах), `hub top`.

## Правила работы с кодом
- Тесты: `.venv/bin/python -m pytest -q` (conftest подменяет HOME; фейковые opencode.db/proc/git/aiogram, без сети).
- Стиль: py3.12, `from __future__ import annotations`, dataclasses, stdlib sqlite3, время параметром (`now`/`clock`),
  комментарии/тексты UI по-русски; чтение — чистые функции. Правила для агентов — `docs/agents/rules.md`.
- Карточки `docs/tasks/*` — формат: Цель, Прочитать, Можно менять, Интерфейс, Приёмка, Нельзя, Сеть, Исполнитель,
  Уровень, Ревью, Коммит (пример — любой `docs/tasks/H1*.md`).
