# agent-hub (Midas-AI-HUB) v2

Сервис, через который оркестратор (Claude Code или другой CLI-агент) и человек раздают работу дешёвым
моделям-работникам (opencode: Muse Spark 1.3 и др.), следят за ней и принимают результат.

Задача проходит путь: очередь → своя копия проекта (git worktree) → работник → ворота (коммит, разрешённые файлы,
тесты) → ревью другими моделями → «готово» → решение оркестратора или человека (принять/слить, доработать, отклонить).
Хаб сам повторяет при сбоях сети, продолжает при тишине, останавливает по бюджету, будит Claude событием, пишет архив
задач в `<проект>/.agent-hub/`. Наблюдатель следит за самим хабом (пульс, логи, прокси, поставщики).

## Установка
```
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
ln -s "$PWD/.venv/bin/ahub" ~/.local/bin/ahub      # команда ahub в PATH
ahub setup ~/путь/к/проекту --claude [--deny deepseek] # .hub.toml v2, реестр проектов, навык для Claude Code
ahub service install && systemctl --user daemon-reload && systemctl --user enable --now ahub ahub-bot
loginctl enable-linger $USER                          # сервис и бот живут без входа в систему
```
Нужны: `opencode` (вход в opencode-go), `claude` (для запуска из Telegram), системный прокси при необходимости
(`HTTPS_PROXY` попадает в юниты при `ahub service install`).

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
- `docs/v2/architecture.md` — согласованная архитектура; `docs/v2/contracts.md` — интерфейсы между частями;
- `docs/v2/plan.md`, `docs/v2/progress.md` — план и ход стройки; `docs/v1/` — старый хаб (история).

## Разработка
`.venv/bin/python -m pytest -q` (≈2.5 мин, без сети; HOME подменяется). Живые тесты с настоящим поставщиком:
`AHUB_LIVE=1 .venv/bin/python -m pytest -m live`.
