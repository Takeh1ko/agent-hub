# agent-hub v2 — контракты между частями

Интерфейсы, на которые опираются карточки M2+. Меняются только осознанно (с правкой этого файла).
Машинные имена состояний/событий/ролей — `ahub/model.py`; поставщики — `ahub/providers/base.py`.

## 1. Рабочая копия задачи

Любая задача (и разведка тоже) работает в своей копии проекта: `git worktree` в `<worktrees>/T<id>`, ветка
`<branch_prefix>T<id>` (по умолчанию `ahub/T12`) от рабочей ветки проекта. Разведка и ревью файлы не меняют —
ворота проверяют, что копия чистая. Служебный каталог в копии — `.ahub/` (в `.git/info/exclude` копии, не в
`.gitignore` проекта):

| Путь | Кто пишет | Что |
|---|---|---|
| `.ahub/result.json` | работник | итог хода по форме §2 |
| `.ahub/report.md` | работник | отчёт для людей/оркестратора (разведка — обязателен) |
| `.ahub/review_r<N>_<model>.json` | ревьюер | вердикт §3 |
| `.ahub/logs/<role>_r<N>[_<model>].log` | хаб | сырой вывод сессии |
| `.ahub/home/` | хаб | изолированное хранилище хаба внутри сессии работника |
| `.ahub/prompt_*.md` | хаб | длинный промпт (> 60 КБ) — работнику передаётся ссылка на файл |

## 2. Итог работника — `.ahub/result.json`

```json
{
  "summary": "1–3 предложения: что сделано / что выяснено",
  "status": "done | blocked",
  "commit": "sha HEAD (код/рутина)",
  "files": ["изменённые файлы (код/рутина)"],
  "tests": {"cmd": "…", "ok": true, "tail": "последние строки вывода"},
  "questions": ["что осталось неясным (коротко)"],
  "notes": "что не сделано / под вопросом"
}
```
- Разведка: `summary` + `.ahub/report.md` (≤ 12 КБ, начинается с раздела «Суть» ≤ 10 строк). `commit/files/tests` — нет.
- Ревью (тип задачи): `summary` + вердикт по форме §3 в `.ahub/review_r1_<model>.json`.
- Код: всё поле `commit` == HEAD, `files` ⊆ дифф, `tests.ok` — приёмка карточки.
- Рутина: как код без `tests`.
- `status: blocked` — работник не может продолжать (нет доступа, противоречие постановки): задача → «Нужно решение».

## 3. Вердикт ревьюера — `.ahub/review_r<N>_<model>.json`

```json
{"verdict": "approve | changes | dispute",
 "findings": [{"severity": "high|medium|low", "file": "путь", "line": 12, "issue": "≤ 300 симв.", "fix": "что сделать"}],
 "summary": "одна фраза"}
```
- `dispute` засчитывается, только если у каждого замечания есть `file`, `line` и `issue` ≥ 50 символов; иначе = changes.
- Панель: все approve → Готово; любой changes → доработка (если круги остались), иначе «Нужно решение».
- Замечания дедуплицируются по (file, line, нормализованный issue); `low` не держит задачу.

## 4. События и доставка

Событие журнала (`event`): `id, ts, task_id, project, kind, payload, needs_reaction, critical, delivered_at, acked_at`.

| kind | Будит оркестратора | Как |
|---|---|---|
| done, needs_decision, error | да | пачкой (окно группировки) |
| owner_message, answer | да | сразу |
| alarm | да | сразу, если `critical`; иначе пачкой |
| остальные (state, phase, session, retry, silence, budget_soft, orphan…) | нет | только журнал |

- **Окно группировки** некритичных — 120 с от первого непросмотренного события (настройка хаба).
- **Доставлено** (`delivered_at`) — событие отдано в поток/wait. **Подтверждено** (`acked_at`) — оркестратор
  его взял. Доставленное, но не подтверждённое, не пропадает: через 30 мин отдаётся повторно, всего не больше
  3 раз (чтобы не будить тем же бесконечно); дальше — видно в `ahub status` («непрочитано») и в сводке потока.
- **Неявное подтверждение** (экономия вызовов): чтение задачи (`ahub status T12`, `ahub result T12`) подтверждает
  её done/needs_decision/error; `ahub inbox` — owner_message/answer; `ahub alarms` — alarm.
  Явно: `ahub ack <id…|all>`.

### Строка пробуждения (L0) — одна на дело, ≤ 200 байт
```
ГОТОВО T12 scout «найти утечку» — отчёт 2.1 КБ, $0.04
РЕШЕНИЕ T13 code «кнопка оплаты» — круги ревью кончились (2 замечания high)
ОШИБКА T14 code «миграция» — подготовка: тесты не собираются
ВЛАДЕЛЕЦ «как там оплата?»
ОТВЕТ #5 «сливать T12?» → да
ТРЕВОГА! opencode недоступен 12 мин (3 задачи ждут)
```

## 5. Уровни подробности и лимиты

| Уровень | Команда | Лимит | Что |
|---|---|---|---|
| L0 | `ahub wait`, `ahub watch` | 200 Б/строка | §4 |
| L1 | `ahub status` | 1500 Б всего | активные (фаза, пульс, модель, круг, $), ждущие решения, открытые вопросы, непрочитанное; при переполнении — счётчики «ещё N» |
| L2 | `ahub status T12`, `ahub result T12` | 4000 Б | задача: цель, состояние/причина, итог работника, проверки (сводка диффа, хвост тестов ≤ 10 строк), замечания без дублей (≤ 10), стоимость одной строкой |
| L3 | `ahub result T12 --full`, `ahub diff T12`, `ahub log T12` | явно; постранично `--max-bytes` (по умолчанию 20000) | полный отчёт, дифф, журнал сессии |

Все команды: `--json` — те же данные машинно (без усечения текста до лимита, но с тем же набором полей).

## 6. Ожидание и присутствие

- `ahub wait [--timeout 30m] [--project P]` — блокируется до события, требующего реакции (с учётом окна группировки);
  печатает L0-строки неподтверждённых событий, помечает доставленными; код 0. Таймаут — пустой вывод, код 3.
- `ahub watch [--project P]` — для Monitor: бесконечный поток L0-строк; позиция — отметки доставки в базе (файла
  состояния нет: перезапуск Monitor ничего не теряет и не повторяет); при старте — одна сводная строка о
  доставленном, но неподтверждённом (не весь хвост).
- **Присутствие**: `wait` и `watch` обновляют `presence(who, project, last_seen, via)` не реже раза в 60 с.
  Claude «есть», если `last_seen` моложе 180 с. На этом — эскалация наблюдателя и запуск Claude из TG.
- `who` — `claude` по умолчанию (`--who` для других оркестраторов).

## 7. Команды оркестратора (CLI, M2)

```
ahub task new --kind scout|code|review|routine --title "цель" (--spec "текст" | --spec-file F)
              [--model spark] [--review "spark,mimo-flash" --rounds 2 | --no-review]
              [--paths "core/**,tests/**"] [--accept "tests/test_x.py::test_y"] [--budget 1.5]
              [--after T3] [--resources test_db] [--input <ветка|sha|a..b|файлы>] [--key K] [--draft]
   → «T12 в очереди» (одна строка); --key — идемпотентность (повтор возвращает ту же задачу)
ahub status [T12]            L1 / L2
ahub result T12 [--full]     L2 / L3
ahub accept T12 | reject T12 [--reason] | rework T12 --notes "…" | stop T12 | continue T12
ahub wait | watch | ack | inbox | say "текст" | ask "вопрос" --options "да,нет" [--task T12] | alarms
ahub models [--role R] | models set …   (реестр; запреты проекта не снимает)
```
Коды выхода: 0 — успех, 2 — отказ (одна строка «ошибка: …» в stderr), 3 — таймаут ожидания.

## 8. Ресурсы проекта

`.hub.toml`: `max_parallel`, `[resources] имя = {capacity, lock}`, `test_resource`. Задача объявляет нужные ресурсы
(`--resources`); очередь не запускает задачу, пока ресурс занят (занятость — живые процессы задач с этим ресурсом;
`lock` — внешний flock, его держатель виден в пульсе). Ожидание места/ресурса — фаза `waiting` с причиной, не тревога.

## 9. Модели по умолчанию (реестр при первом запуске)

| alias | поставщик / модель / вариант | роли (меню) |
|---|---|---|
| spark | opencode / opencode-go/muse-spark-1.3-contributor / xhigh | executor★, reviewer★, scout★, routine★ |
| spark-high | opencode / opencode-go/muse-spark-1.3-contributor / high | observer★, drafter★ |
| spark-medium | opencode / opencode-go/muse-spark-1.3-contributor / medium | observer |
| mimo-flash | opencode / opencode-go/mimo-v2.6-flash / — | executor, reviewer, routine |
| deepseek-flash | opencode / opencode-go/deepseek-v4.1-flash / high | executor, reviewer, scout |
| spark-free | opencode / opencode/muse-spark-1.3-contributor-free / xhigh | (вне меню, доступна явно) |
| gemini | agy / gemini-3.8-flash-high / — | (вне меню до V29) |
★ — по умолчанию в роли.
