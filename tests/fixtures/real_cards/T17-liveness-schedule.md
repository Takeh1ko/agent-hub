# T17 — живучесть воркера рынка и расписание по важности

**Цель.** docs/market/critique_v2.md (блокеры 1, 3, 4, 5, 6 и решения архитектора): воркер сутками без присмотра —
не теряет проходы, не жжёт квоту на больных категориях, не распухает, укладывается в пропускную способность одного прокси.

**Прочитать.** docs/market/critique_v2.md, `worker/market_pk.py`, `core/market/pipeline.py` (`run_category`, `finish_crawl`,
`save_category_daily`), `core/modules/market/pk_store.py` (`pk_crawls`, `pk_categories`, `purge_listing`),
`core/modules/market/migrations/*.sql`, `core/market/read.py` (`emit_*`), `core/modules/events/kinds.py`,
`sales.sale_logs`/`lots.lots` (как считать продажи категории за 30 дней — только чтение, через функцию-обёртку, чтобы
тесты подменяли).

**Можно менять.** `worker/market_pk.py`, `core/modules/market/pk_store.py` (новые методы), `core/market/pipeline.py`
(только `finish_crawl` в `finally` и daily для search), `core/modules/market/migrations/0009_pk_liveness.sql` (новый;
0008 занят T16), `core/modules/events/kinds.py` (новые виды `market.*`), `tests/test_market_worker.py`,
`tests/test_market_pk_store.py`, `tests/test_market_pipeline.py`, `docs/market/spec.md`.

**Интерфейс.**
1. **Свипер:** в начале цикла воркера проходы без `finished_at` старше 2 ч → `aborted=1, finished_at=now()`
   (`PkStore.sweep_stale_crawls`). `run_category`: исключение → `finish_crawl(aborted=1, errors+1)` в `finally`.
2. **Backoff больных категорий:** `pk_categories.fail_count`, `retry_after`; ошибка/обрыв → `retry_after = now + min(6 ч,
   15 мин × 2^fail_count)`; удачный проход → сброс. Воркер пропускает категории до `retry_after`.
3. **Расписание по важности:** раз в сутки `crawl_every_min` для каждой категории: топ-15 по выручке наших продаж за 30 дней
   — 60; остальные с нашими продажами/лотами — 180; прочие — 360 (константы). Функция `category_priorities(db)` читает
   продажи; в тестах подменяется.
4. **Heartbeat:** событие `market.worker_heartbeat` раз в час (категорий пройдено/отстаёт/больных); `market.worker_stalled`
   — если за 3 ч ни одного целого прохода.
5. **Ретеншн:** `purge(days=90)` чистит `pk_listing`, `pk_price_log`, `pk_rank`, `pk_crawls` (каскадом) старше 90 дней;
   `pk_verdicts`/`pk_labels` не текущей логики старше 30 дней; дата последней чистки — в `pk_categories`-соседней
   таблице `market.pk_meta(key, value)`, не в памяти.
6. **Daily для search:** `save_category_daily` и для целого прохода в режиме search (по объединению поисков).

**Приёмка.**
- Незакрытый проход 3-часовой давности → закрыт свипером; исключение в `run_category` → проход закрыт `aborted`.
- 3 ошибки подряд → `retry_after` растёт 15/30/60 мин; удача → сброс.
- `category_priorities` на фейковых продажах → 60/180/360 как описано.
- Heartbeat/stalled события на фейковых часах.
- Ретеншн удаляет старое, не трогает свежее; `pk_meta` хранит дату.
- search-проход пишет `category_daily`.
- `pytest -q tests/test_market_worker.py tests/test_market_pk_store.py tests/test_market_pipeline.py tests/test_market_read.py`

**Нельзя.** Менять `classify.py`, `llm.py`, `sales.py`, `rank.py`, `crawl.py`; устанавливать/включать сервис; сеть;
боевая БД (продажи читать только через подменяемую функцию); `bot.deps`.

**Сеть.** нет.

**Исполнитель.** muse.

**Коммит.** `feat(market): живучесть воркера — свипер, backoff, расписание по важности, heartbeat, ретеншн`

## Решения арбитра (круг 3)

1. **MEDIUM — принято.** Heartbeat/stalled проверять **после каждой категории** (по времени, не чаще раза в час), не в
   конце цикла. Тест: цикл из 3 категорий по «2 часа» фейковых часов → 2+ heartbeat.
2. **MEDIUM — принято.** `due`/`lagging` в heartbeat — снимок **до** прогона (сколько было к обходу и сколько не успели).
3. **MEDIUM — принято.** Топ-15 — только среди категорий из `pk_categories` (обходимых); остальные категории продаж не
   участвуют. Тест.
4. Low — принять: логировать исключения в heartbeat; убрать мёртвый try/except вокруг `events.emit`; stalled не
   слать, пока не было ни одного целого прохода (или с явной пометкой «ещё не было»); `list_due_categories` отсчитывает
   от последнего **целого** прохода (оборванный/сметённый не откладывает перепроход на весь `crawl_every_min`); тест
   успешного `refresh_priorities`; тест «нецелый search-проход daily не пишет».
