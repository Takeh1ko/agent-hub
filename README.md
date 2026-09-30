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
ln -s "$PWD/.venv/bin/hub" ~/.local/bin/hub          # команда hub (= ahub) в PATH
hub setup ~/путь/к/проекту --claude [--deny deepseek] # .hub.toml v2, реестр проектов, навык для Claude Code
hub service install && systemctl --user daemon-reload && systemctl --user enable --now ahub ahub-bot
loginctl enable-linger $USER                          # сервис и бот живут без входа в систему
```
Нужны: `opencode` (вход в opencode-go), `claude` (для запуска из Telegram), системный прокси при необходимости
(`HTTPS_PROXY` попадает в юниты при `hub service install`).

## Пользование
| Кто | Как |
|---|---|
| Человек, терминал | `hub top` (экран; `c` — управление, `?` — справка), `hub status`, `hub history`, `hub draft new "задача словами" -P проект` → `hub draft start N` |
| Человек, Telegram | текст боту — сообщение Claude (нет живой сессии — хаб поднимет Claude); `/tasks`, `/status`, `/help` |
| Claude Code | навык `ahub`: Monitor на `hub watch`; `hub task new …`; по событию `hub status T12` → `hub accept / rework / reject` |
| Другие агенты | MCP-сервер `hub mcp` (stdio) — те же ручки |

Главные команды: `hub task new --kind scout|code|routine|review --title … --spec …`, `hub status [T12]`,
`hub result T12 [--full]`, `hub accept|reject|rework|continue|stop T12`, `hub budget T12 --add 1`,
`hub models`, `hub service status|pause|resume`, `hub observer reports`. Всё — `hub --help`.

## Документация
- `docs/ARCHITECTURE.md` — карта кода (с неё начинать);
- `docs/v2/architecture.md` — согласованная архитектура; `docs/v2/contracts.md` — интерфейсы между частями;
- `docs/v2/plan.md`, `docs/v2/progress.md` — план и ход стройки; `docs/v1/` — старый хаб (история).

## Разработка
`.venv/bin/python -m pytest -q` (≈2.5 мин, без сети; HOME подменяется). Живые тесты с настоящим поставщиком:
`AHUB_LIVE=1 .venv/bin/python -m pytest -m live`.
