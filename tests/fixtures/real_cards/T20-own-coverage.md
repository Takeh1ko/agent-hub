# T20 — полное покрытие наших лотов: самосопоставление с паспортом и поиск по нашим лотам

**Цель.** docs/market/recon_coverage_gaps.md (R04): из 240 наших лотов в отслеживаемых категориях без места ~55:
28 — поисковая категория не вернула наш лот, 22 — паспорт игры есть, но slug нашего лота в нём не заведён, 4 — нет
паспорта игры. Паспорта (`core/sourcing/v2/`) **не трогать** — это код закупа. Покрыть силами модуля рынка.

**Прочитать.** docs/market/recon_coverage_gaps.md, `core/market/pipeline.py` (`read_own_lots`, `pick_passports`, `own_by_target`,
`_search_pass`, `pk_own`), `core/market/classify.py` (`classify`, `prefilter`, разметка по паспорту), `core/modules/market/pk_store.py`,
`core/sourcing/v2/passports/*.json` (только читать).

**Можно менять.** `core/market/pipeline.py`, `core/modules/market/pk_store.py` (новые методы),
`core/modules/market/migrations/0013_pk_own_map.sql` (новый), `tests/test_market_pipeline.py`, `tests/test_market_pk_store.py`,
`docs/market/spec.md`.

**Интерфейс.**
1. **Самосопоставление:** наш лот категории, slug/item_id которого нет в карточках паспортов, но заголовок проходит
   `prefilter` паспорта какой-то игры → классифицировать **наш** лот против всех целей этого паспорта той же процедурой,
   что конкурентов (разметка по паспорту, правила) → цели с MATCH становятся целями нашего лота
   (`market.pk_own_map(item_id, target_id, source='self_match'|'passport', decided_at)`). Паспортная привязка — в приоритете.
   Лот без MATCH ни к одной цели — в отчёт прохода «наши лоты без цели» (название, причина).
2. **Поиск по нашим лотам (режим search):** кроме `search_queries` паспортов, для каждого нашего лота категории — запрос
   по его названию без эмодзи/служебных слов (2–4 значимых слова: игра + издание); результаты — в объединение поиска.
   Если наш лот всё равно не найден — `own_missing` с причиной в отчёте.
3. Отчёт прохода (`SliceReport.to_markdown`): «наших лотов: всего N, с целью M (паспорт K, самосопоставление S), без
   цели X, не найдено в выдаче Y».

**Приёмка (без сети).**
- Наш лот без slug в паспорте, заголовок той же игры, фейковая разметка MATCH к цели → место посчитано, `pk_own_map`
  `self_match`.
- Наш лот чужой игры без паспорта → «без цели», не падает.
- Search-категория: наш лот не попадает в паспортные запросы, но находится запросом по его названию → место есть.
- `pytest -q tests/test_market_pipeline.py tests/test_market_pk_store.py tests/test_market_slice.py tests/test_market_worker.py`

**Нельзя.** Менять `core/sourcing/v2/`, `classify.py`, `llm.py`, `crawl.py`, `pk_client.py`, `worker/`; сеть; боевая БД
(наши лоты — через `read_own_lots`, в тестах подменяется).

**Сеть.** нет.

**Исполнитель.** muse.

**Коммит.** `feat(market): полное покрытие наших лотов — самосопоставление с паспортом, поиск по нашим лотам`

## Решения арбитра (круг 3)

Принять high/medium ревью круга 2 (`.agent.prev_*/review_r2.json`), с тестами:
1. **HIGH.** `run_category(..., llm=None)` не падает на самосопоставлении (опечатка `self_call`/`_self_call`); тест:
   непривязанный лот + llm=None → проход завершается, лот «без цели» с причиной PENDING.
2. **MEDIUM.** Тестовые хелперы пайплайна патчат `pipeline_mod.load_all → {}` (как OwnCoverageTest) — без +15 с на набор.
3. **MEDIUM.** Липкая прошлая привязка — только если свежих REJECT нет (PENDING/UNSURE/ошибка — да, REJECT — нет:
   такой лот «без цели» с причиной). Как в spec §4.4.
4. **MEDIUM.** `test_other_category_pk_own_survives` — на реальный extra-путь (`pick_passports → ([], set())`, паспорт через
   `load_all`), чужая строка `pk_own` на той же цели переживает проход.
5. Ворота были красными — добиться зелёной приёмки.
