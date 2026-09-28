# H03 — ворота + done.json + repair-промпт + вердикт панели

**Цель.** Библиотека ворот: в `ready` проходит только закоммиченная работа с зелёной приёмкой,
диффом внутри «Можно менять» и валидным `.agent/done.json`. Мелкий провал чинится repair-промптом
в ту же сессию, а не новым кругом ревью.

**Прочитать.** docs/spec.md (§7 ворота, repair, вердикт панели), docs/agents/rules.md, карточку
docs/tasks/H01-core-read.md (интерфейс: `Store.get_task`, `hub/read/git.py`
`branch_commits/diff_stat`, `hub/read/procs.py::lock_holder`, `hub/commands/<имя>.py` с `register`).
Карточку H02 — только читать (границы glob'ов и `lock_holder`).

**Можно менять.** `hub/gate/gate.py`, `hub/gate/donefile.py`, `hub/gate/repair.py`,
`hub/gate/verdict.py`, `hub/commands/gate.py`, `tests/test_gate.py`, `tests/test_verdict.py`,
`tests/fixtures/gate/**` (только свои фикстуры). `hub/gate/__init__.py` не трогать (владелец — H02).
Миграции не требуются и не создаются.

**Интерфейс (контракт для следующих задач — не переименовывать).**
1. `hub/gate/donefile.py`: `@dataclass DoneFile(commit: str, files: list[str], cmd: str, ok: bool,
   tail: str, notes: str)`; `load_done(worktree: Path) -> DoneFile` — читает
   `<worktree>/.agent/done.json`, проверяет схему `{"commit": sha, "files": [..], "tests": {"cmd": "..",
   "ok": true, "tail": ".."}, "notes": ".."}`; при отсутствии/битом JSON — `FileNotFoundError` /
   `ValueError` с текстом `done.json: <что не так>` (какое поле, каким должно быть).
2. `hub/gate/gate.py`: `@dataclass GateResult(ok: bool, errors: list[str], diff_stat: str,
   tests_tail: str)`; `check_gate(repo: Path, base_sha: str, head: str = "HEAD",
   allowed: list[str] = ..., test_cmd: list[str] = ..., lock_path: str | None = None,
   timeout_s: int = 600) -> GateResult` — порядок: `base..HEAD` не пуст (иначе `empty-diff`);
   файлы диффа (`git diff --name-only base..head`) ⊆ `allowed` (`fnmatch`, иначе `forbidden: <путь>`);
   приёмка `test_cmd` под замком проекта (`flock`-файл `lock_path`, для тестов самого hub —
   `hub-selftest.lock`; занят → `locked: pid <pid>`, команду не запускаем); код приёмки ≠ 0 →
   `tests-fail: <хвост ≤ 2000 симв.>`; `diff_stat` — `git diff --stat`, `tests_tail` — хвост вывода.
3. `hub/gate/repair.py`: `REPAIR_PROMPT: str` — фиксированный текст (без f-строк с контекстом задачи):
   «git status --short; закоммить поимённо (git add <пути>, никогда -A); запиши .agent/done.json по схеме;
   верни HEAD»; `repair_prompt(reason: str) -> str` возвращает `REPAIR_PROMPT + "\nПричина: " + reason`.
4. `hub/gate/verdict.py`: `@dataclass Review(verdict: str, file: str = "", line: int = 0,
   body: str = "")` (`verdict ∈ {approve, changes, dispute}`); `verdict(reviews: list[Review],
   round: int, max_rounds: int = 2) -> str` — `dispute` без `file:line` или с обоснованием
   короче 50 символов считается `changes`; все `approve` → `"ready"`; есть (настоящий) `changes` →
   `"next"` если `round < max_rounds`, иначе `"arbiter"`; только валидные `dispute` без `changes` →
   `"arbiter"` сразу.
5. `hub/commands/gate.py`: `register(subparsers)` → `hub gate ID [--project ROOT] [--round N]`;
   читает задачу из `Store` (ветка/worktree/base_sha), карточку задачи (раздел «Можно менять» —
   через `hub.gate.lint` H02, только импорт, без копирования), `load_done` + сверка `commit == HEAD`
   (`mismatch: done=<sha> head=<sha>`) и `files ⊆ diff` (`unknown-file: <путь>`), затем `check_gate`;
   exit 0 + `OK <id>`, иначе exit 1 и каждый элемент `errors` с новой строки.

**Приёмка.**
- `pytest -q tests/test_gate.py tests/test_verdict.py`
- `tests/test_gate.py` на фейковом git-репо: пустой `base..HEAD` → `empty-diff`; файл вне allowed →
  `forbidden:`; падающая приёмка → `tests-fail:` с хвостом; занятый замок → `locked:` и команда
  не запускалась (маркер-файл отсутствует); `load_done` — нет файла → `FileNotFoundError`,
  нет поля `tests.ok` → `ValueError` с именем поля; `hub gate`-сверка: `commit != HEAD` →
  `mismatch:`, файл из done.json вне диффа → `unknown-file:`.
- `tests/test_verdict.py`: все approve → `ready`; changes в круге 1 → `next`, в круге 2 → `arbiter`;
  dispute с `file:line` и телом ≥ 50 симв. без changes → `arbiter`; dispute без файла или с телом
  10 симв. → как changes (`next`/`arbiter` по кругу).
- `repair_prompt("нет коммита")` содержит `git status`, запрет `-A`, `done.json` и строку
  `Причина: нет коммита`.

**Нельзя.** Менять `hub/store.py`, `hub/config.py`, `hub/read/**`, `hub/secrets.py`, `hub/tg_send.py`;
создавать миграции; трогать файлы H02 (`hub/gate/__init__.py`, `hub/gate/lint.py`,
`hub/gate/preflight.py`, `hub/commands/lint.py|preflight.py`), H04 (`hub/read/events.py`,
`hub/read/findings.py`, `hub/commands/wait.py|findings.py|inbox.py|ask.py|say.py`,
`hub/migrations/002_*.sql`), H07 (`hub/tui/**`, `hub/commands/top.py`); копировать логику линтования
в свои модули — только импорт из `hub.gate.lint`; добавлять зависимости; сеть.

**Сеть.** нет. **Уровень.** medium. **Исполнитель.** musefree.

**Коммит.** `feat: H03 ворота, done.json, repair-промпт и вердикт панели`
