# H01 — ядро: время, конфиг, хранилище, слой чтения, `hub status/roster/cost`

**Цель.** Одна команда `hub status` даёт Claude всю картину работы агентов (≤ 1,5 КБ), `hub roster` — кто в какой роли
над чем, `hub cost` — деньги. Фундамент для H02–H08: они только добавляют файлы в `hub/commands/`, `hub/migrations/`.

**Прочитать.** docs/spec.md (§3–§6, §8, §11), docs/agents/rules.md, образец текущего конвейера:
/home/takehiko/Projects/Python/PlayerUP/tools/agents/run_task.py (как устроены `.agent/state.json`, review_rN.json)
и /home/takehiko/Projects/Python/PlayerUP/tools/agents/cost.py (как читается opencode.db). Живая opencode.db —
`~/.local/share/opencode/opencode.db` — только для изучения схемы, строго `mode=ro`; в тестах — фейк.

**Можно менять.** `hub/**` (кроме `hub/secrets.py`), `tests/**`, `pyproject.toml` (только `[project.scripts]`),
`.hub.toml`, `docs/examples/**`.

**Интерфейс (контракт для следующих задач — не переименовывать).**
1. `hub/time.py`: `now_ms() -> int`; `to_local(ms) -> datetime` (Asia/Yekaterinburg); `fmt_local(ms) -> "23:41"` /
   `"28.09 23:41"` если не сегодня; `parse_since(text, now_ms) -> int` понимает `"2026-09-28 20:00"` (локальное),
   `"сегодня 20:00"`, `"2ч"`, `"30м"`, `"1д"`.
2. `hub/config.py`: `ProjectConfig` (dataclass, поля как §11 spec, `$HOME` разворачивается) ; `load_project(path) ->
   ProjectConfig` (ищет `.hub.toml` вверх от path); `load_projects() -> list[ProjectConfig]` из
   `~/.config/agent-hub/config.toml` (`projects = ["~/Projects/Python/PlayerUP", …]`); нет файла → пустой список.
   Добавь `.hub.toml` для самого agent-hub в корень репозитория и `docs/examples/PlayerUP.hub.toml` (не в PlayerUP).
3. `hub/store.py`: `Store(path=None)` — `$AGENT_HUB_HOME/hub.db` (по умолчанию `~/.local/share/agent-hub`), WAL;
   миграции — файлы `hub/migrations/NNN_<имя>.sql`, применяются по порядку, учёт в таблице `migration`.
   `001_core.sql` — таблицы `task`, `session`, `event`, `question`, `inbox`, `budget` из §4 spec (`inbox(id, ts, text,
   source, seen_claude)`). Методы: `upsert_task(**f)`, `get_task(id)`, `list_tasks(active_only=True)`,
   `link_session(external_id, tool, task_id, role, round, model)`, `add_event(task_id, kind, payload)`,
   `events_since(id)`. Импорт задач старого конвейера: `import_legacy(worktrees_dir)` — читает
   `<wt>/*/.agent/state.json` (+ review_rN*.json) и заводит/обновляет задачи (этап из status/round/verdicts), роль
   сессий — executor/reviewer по state.json.
4. `hub/read/opencode.py`: `sessions(db_path, since_ms, directory_prefix=None) -> list[OcSession]` —
   `OcSession(id, directory, title, model, provider, started_ms, pulse_ms, steps, tokens_in, tokens_out, cache_read,
   cache_write, cost, context_tokens, active_tool, active_tool_age_s, last_activity)`; пульс — max time_updated по
   message/part/todo; `active_tool` — последний part type=tool со `state.status` pending/running; `last_activity` —
   «bash: pytest -q …» / «edit worker/x.py» / «думает» (≤ 60 симв.); `go` vs `usd`: провайдер `opencode-go` → go.
   Незнакомая схема → `[]` + предупреждение в лог, не исключение.
5. `hub/read/procs.py`: `agent_procs() -> list[Proc(pid, kind{opencode|agy|run_task|pytest|flock}, cwd, args,
   started_ms, children)]` по `/proc` (путь `/proc` параметром для тестов); `lock_holder(lock_path) -> Proc | None`.
6. `hub/read/git.py`: `branch_commits(repo, base, branch) -> int`, `diff_stat(repo, base, head) -> str`,
   `is_dirty(worktree) -> bool`, `worktrees(repo) -> list[dict]` (`--porcelain`).
7. `hub/read/snapshot.py`: `build(store, now_ms, opencode_db=None, proc_root="/proc") -> Snapshot`; в Snapshot —
   задачи с сессиями (роль, модель, пульс-индикатор 🟢🟡🔴⚫ по §5, $ go/usd, контекст, последнее действие),
   итоги $ за сегодня; `Snapshot.to_text(limit=1500)`, `Snapshot.to_json()`, `Snapshot.roster_text()`
   (модель → роль → задача → этап → пульс → $).
8. `hub/cli.py`: `main()`; подкоманды — модули `hub/commands/<имя>.py` с `register(subparsers)` (автообнаружение).
   Команды H01: `status [--json] [--all]`, `roster`, `cost [--since] [--by task|model|role|day]`, `import-legacy
   [--worktrees DIR]`. `pyproject.toml`: `hub = "hub.cli:main"`.

**Приёмка.**
- `pytest -q tests/`
- Тесты на фейковой opencode.db (создать схему как в живой: session/message/part/todo с JSON в `data`): пульс, активный
  tool, go/usd раздельно, контекст = input + cache.read последнего assistant; незнакомая схема → `[]`.
- Фейковый `/proc` (каталог с `<pid>/cmdline`, `cwd`-симлинк, `stat`): распознаются opencode/pytest/flock, держатель замка.
- `import_legacy` на фейковом worktree с state.json → задача с правильным этапом.
- `hub status` на фейковых данных ≤ 1500 байт при 20 задачах; пульс 🔴 при exec без пульса 21 мин и без объяснения,
  🟡 при активном pytest-ребёнке.
- `parse_since` — все форматы, локальная зона.

**Нельзя.** Писать в чужие БД; держать соединения к opencode.db дольше запроса; сеть; менять `hub/secrets.py`,
`hub/tg_send.py`; добавлять зависимости.

**Сеть.** нет. **Уровень.** hard. **Исполнитель.** muse.

**Коммит.** `feat: ядро agent-hub — время, конфиг, хранилище, слой чтения, hub status/roster/cost`

## Решения арбитра (круг 3)

1. **HIGH — принято, ошибка карточки:** `.hub.toml` и `docs/examples/**` добавлены в «Можно менять» — ворота теперь
   их пропустят; сами файлы оставить.
2. Принять **все** замечания ревью круга 2 (`.agent.prev_*/review_r2.json`), с тестами на каждое:
   незнакомая схема opencode.db → `[]` + warning (try/except sqlite3.Error вокруг запросов; сверять и колонки);
   контекст — последний assistant **с ненулевым** input+cache.read и без error; `_legacy_stage` использует verdicts
   (failed + changes → `review rN`); `_last_review_verdict` читает `review_rN*.json` (в т. ч. `review_rN_<имя>.json`,
   приоритет dispute > changes > approve); sentinel-сессии `noop`/`panel` не импортировать; тест 🟡 при pytest-ребёнке
   с пульсом 21 мин (без ребёнка — 🔴) и ветка «жив, 21 мин, без объяснения → 🔴»; `hub cost` — тест с известными
   деньгами и точным выводом по model/role/task/day; `upsert_task` не трогает `created_at` при обновлении; порог
   pytest 30 мин — по факту pytest/flock-ребёнка, не по имени этапа; `read/git._run` ловит OSError, каталог без .git —
   «грязный»; `hub cost --since мусор` → сообщение и код 2 (так же в любом CLI с parse_since); `to_text` режет по байтам.
3. Коммит — после каждого блока правок; в конце `git status` чистый.
