# agent-hub v2 — карта кода (шпаргалка для Claude)

Сжатая карта, чтобы не изучать проект заново. Архитектура — `docs/architecture.md`,
интерфейсы между частями — `docs/contracts.md`.

## Что это
Сервис, через который оркестратор (Claude Code; любой CLI-агент — через CLI или MCP) и человек (терминал `ahub top`,
Telegram) раздают работу дешёвым моделям-работникам (opencode: Spark 1.3 и др.), следят за ней и принимают
результат. Команда `ahub` (пакет ahub; скрипт `hub` убран из пакета — конфликт с GitHub CLI `hub`).

## Как течёт задача
```
ahub task new (tasks.py: проверка полей, умолчания по типу)  → task: queued
ahub service (service.py, systemd ahub.service): очередь, места, ресурсы, «после X» → spawn `python -m ahub.worker T12`
worker.py → engine.py (владелец задачи, аренда):
  разведка:  prepare(копия) → working → итог по форме (.ahub/result.json + report.md) → done
  код/рутина: prepare.py (копия без секретов, хук, сбор приёмки) → working → checking (gates.py: коммит, дифф ⊆ paths,
             result.json, приёмка под замком) → reviewing (review.py: панель в новых сессиях) → fixing → … → done
  итоги хода (providers/runner.py → Outcome): сбой сети → повтор; тишина → одно продолжение; квота/таймаут/бюджет →
  needs_decision; ошибка → error; стоп → stopped
events.py: коды DONE/DECISION/ERROR/OWNER/ANSWER/ALARM (ALARM! — критичная) → Claude будит `ahub watch` (Monitor) / `ahub wait`
accept.py: ahub accept (разведка — принять; код — merge --no-ff в рабочую ветку, приёмка, откат при красной, push,
  уборка копии, архив), rework / reject / continue / task edit / extend / budget / model
```
Состояния и переходы — `ahub/model.py` (единственный источник имён); переходы и аренда — `ahub/transitions.py`.

## Модули `ahub/`
| Модуль | Роль |
|---|---|
| `cli.py`, `cliutil.py`, `commands/*.py` | CLI: автообнаружение `register()`; `--json`; ошибки — одна строка, код 2 |
| `config.py`, `paths.py` | `.hub.toml` v2 (v1 читается с переводом), `~/.config/ahub/config.toml` ([telegram] токен/чат/прокси, [usage] лимит Go, [paths] opencode/claude/opencode_db); данные `~/.local/share/ahub/ahub.db`, логи `~/.local/state/ahub/logs` (`AHUB_HOME` — всё в одном каталоге) |
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
| `tui/` | `ahub top` (`data.py` данные, `app.py` textual) |
| `mcp.py` | MCP-сервер (stdio) поверх тех же ручек |
| `claude/SKILL.md` | навык для Claude Code (ставит `ahub setup --claude`) |

## Процессы
- Служба ОС (`ahub service install`): Linux — юниты systemd --user `ahub.service` + `ahub-bot.service`
  (`commands/service.py`: `ExecStart=<python> -m ahub service|bot run`, `Restart=always`, `KillMode=process`);
  macOS — plist launchd `dev.ahub.service.plist` + `dev.ahub.bot.plist` в `~/Library/LaunchAgents`
  (`Label`, `ProgramArguments`, `RunAtLoad` + `KeepAlive`, то же окружение, логи в `state/logs`;
  включение — `launchctl bootstrap gui/$(id -u) <путь>`). Юнит/plist бота — только если включён Telegram
  (`[telegram] token`), иначе строка «бот не установлен: нет [telegram] token».
- Без службы ОС: `ahub service start` — `service run` фоном (`start_new_session`, лог `state/logs/service.log`,
  pid в `service_pid_path()` каталога данных); уже жив pid или тик сердцебиения < 30 с — второй не запускается.
  `ahub service stop` — SIGTERM по pid-файлу, ждать до 10 с, файл удалить.
- Процессы задач — отдельные (`python -m ahub.worker T<id>`), переживают перезапуск сервиса.
- `ahub/procs.py` — дети, живость, cmdline, время старта: Linux через /proc, иначе psutil.
- Claude: Monitor на `ahub watch`; `ahub status`; решения — `ahub accept|rework|reject`.

## Работа с кодом
- Тесты: `.venv/bin/python -m pytest -q` (~2.5 мин; HOME подменяется, сети нет); живые: `AHUB_LIVE=1 … -m live`.
- Стиль: py3.11+, `from __future__ import annotations`, dataclasses, stdlib sqlite3, время параметром, по-русски.
- Задачи для самого agent-hub тоже можно гонять через хаб (`.hub.toml`: рабочая ветка `main`).
