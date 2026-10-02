# agent-hub (Midas-AI-HUB) v2

Сервис, через который оркестратор (Claude Code или другой CLI-агент) и человек раздают работу дешёвым
моделям-работникам (opencode: Muse Spark 1.3 и др.), следят за ней и принимают результат.

Задача проходит путь: очередь → своя копия проекта (git worktree) → работник → ворота (коммит, разрешённые файлы,
тесты) → ревью другими моделями → «готово» → решение оркестратора или человека (принять/слить, доработать, отклонить).
Хаб сам повторяет при сбоях сети, продолжает при тишине, останавливает по бюджету, будит Claude событием, пишет архив
задач в `<проект>/.agent-hub/`. Наблюдатель следит за самим хабом (пульс, логи, прокси, поставщики).

## Установка
```
pipx install ahub
ahub setup
ahub doctor
```
Мастер `ahub setup` спросит язык, проект, модели, службу, навык Claude и Telegram
(без вопросов — `ahub setup --yes`). Проверка — `ahub doctor`.

## Пользование
| Кто | Как |
|---|---|
| Человек, терминал | `ahub top` (экран; `c` — управление, `?` — справка), `ahub status`, `ahub history`, `ahub draft new "задача словами" -P проект` → `ahub draft start N` |
| Человек, Telegram | текст боту — сообщение Claude (нет живой сессии — хаб поднимет Claude); `/tasks`, `/status`, `/help` |
| Claude Code | навык `ahub`: Monitor на `ahub watch`; `ahub task new …`; по событию `ahub status T12` → `ahub accept / rework / reject` |
| Другие агенты | MCP-сервер `ahub mcp` (stdio) — те же ручки |

Главные команды: `ahub task new --kind scout|code|routine|review --title … --spec …`, `ahub status [T12]`,
`ahub result T12 [--full]`, `ahub accept|reject|rework|continue|stop T12`, `ahub budget T12 --add 1`,
`ahub models`, `ahub service status|pause|resume`, `ahub observer reports`. Всё — `ahub --help`.

## Документация
- `docs/ARCHITECTURE.md` — карта кода (с неё начинать);
- `docs/architecture.md` — архитектура; `docs/contracts.md` — интерфейсы между частями.

## Разработка
`.venv/bin/python -m pytest -q` (≈2.5 мин, без сети; HOME подменяется). Живые тесты с настоящим поставщиком:
`AHUB_LIVE=1 .venv/bin/python -m pytest -m live`.
