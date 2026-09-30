# agent-hub v2 — карта кода (шпаргалка для Claude)

Сжатая карта, чтобы не изучать проект заново. Согласованная архитектура — `docs/v2/architecture.md`,
интерфейсы между частями — `docs/v2/contracts.md`, история стройки — `docs/v2/progress.md`, v1 — `docs/v1/`.

## Что это
Сервис, через который оркестратор (Claude Code; любой CLI-агент — через CLI или MCP) и человек (терминал `hub top`,
Telegram) раздают работу дешёвым моделям-работникам (opencode: Spark 1.3 и др.), следят за ней и принимают
результат. Команды `hub` и `ahub` — одно и то же (`ahub/cli.py`).

## Как течёт задача
```
hub task new (tasks.py: проверка полей, умолчания по типу)  → task: queued
hub service (service.py, systemd ahub.service): очередь, места, ресурсы, «после X» → spawn `python -m ahub.worker T12`
worker.py → engine.py (владелец задачи, аренда):
  разведка:  prepare(копия) → working → итог по форме (.ahub/result.json + report.md) → done
  код/рутина: prepare.py (копия без секретов, хук, сбор приёмки) → working → checking (gates.py: коммит, дифф ⊆ paths,
             result.json, приёмка под замком) → reviewing (review.py: панель в новых сессиях) → fixing → … → done
  итоги хода (providers/runner.py → Outcome): сбой сети → повтор; тишина → одно продолжение; квота/таймаут/бюджет →
  needs_decision; ошибка → error; стоп → stopped
events.py: done/needs_decision/error/owner_message/answer/alarm → Claude будит `hub watch` (Monitor) / `hub wait`
accept.py: hub accept (разведка — принять; код — merge --no-ff в рабочую ветку, приёмка, откат при красной, push,
  уборка копии, архив), rework / reject / continue / task edit / extend / budget / model
```
Состояния и переходы — `ahub/model.py` (единственный источник имён); переходы и аренда — `ahub/transitions.py`.

## Модули `ahub/`
| Модуль | Роль |
|---|---|
| `cli.py`, `cliutil.py`, `commands/*.py` | CLI: автообнаружение `register()`; `--json`; ошибки — одна строка, код 2 |
| `config.py`, `paths.py` | `.hub.toml` v2 (v1 читается с переводом), `~/.config/ahub/config.toml`; данные `~/.local/share/ahub/ahub.db`, логи `~/.local/state/ahub/logs` (`AHUB_HOME` — всё в одном каталоге) |
| `store.py` + `migrations/` | SQLite WAL: task, task_dep, session, event (доставка/подтверждение), question, message, draft, model/role_model, presence, claude_launch, observer_report, op |
| `model.py`, `transitions.py` | типы, состояния, переходы, события; move/acquire/renew/release/request_stop/once |
| `tasks.py`, `drafts.py` | создание задачи с проверкой; черновик словами → модель → предпросмотр → запуск |
| `registry.py` | модели (alias → поставщик/модель/вариант), меню ролей, запреты проекта |
| `providers/` | `base.py` контракт; `runner.py` общий запуск (вывод в файл — opencode теряет хвост в пайп; тишина с учётом детей; стоп группой); `opencode.py`, `opencode_db.py`; `fake.py` для тестов |
| `workspace.py`, `prepare.py`, `gates.py`, `review.py`, `prompts.py` | копия/ветка, подготовка, ворота, панель ревью, промпты |
| `engine.py`, `worker.py` | ход задачи, процесс задачи |
| `service.py` | очередь, сироты, самообновление на новый код, сердцебиение, поток наблюдателя |
| `events.py`, `comms.py`, `views.py`, `archive.py` | доставка/присутствие; сообщения/вопросы/тревоги; L1–L3 с лимитами; архив `<проект>/.agent-hub/` |
| `pulse.py`, `observer.py` | пульс 🟢🟡🔴⚫⚪; наблюдатель (5 мин код, 30 мин модель, прокси Koala, эскалация) |
| `tg/` | бот (`core.py` логика, `run.py` aiogram, `launcher.py` запуск Claude без живой сессии, `proxy.py`) |
| `tui/` | `hub top` (`data.py` данные, `app.py` textual) |
| `mcp.py` | MCP-сервер (stdio) поверх тех же ручек |
| `claude/SKILL.md` | навык для Claude Code (ставит `hub setup --claude`) |

## Процессы
- `systemctl --user … ahub.service` — `hub service run` (очередь + наблюдатель); `ahub-bot.service` — `hub bot run`.
- Процессы задач — отдельные (`python -m ahub.worker T<id>`), переживают перезапуск сервиса.
- Claude: Monitor на `hub watch`; `hub status`; решения — `hub accept|rework|reject`.

## Работа с кодом
- Тесты: `.venv/bin/python -m pytest -q` (~2.5 мин; HOME подменяется, сети нет); живые: `AHUB_LIVE=1 … -m live`.
- Стиль: py3.12, `from __future__ import annotations`, dataclasses, stdlib sqlite3, время параметром, по-русски.
- Задачи для самого agent-hub тоже можно гонять через хаб (`.hub.toml`: рабочая ветка `main`).
