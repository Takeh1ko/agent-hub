# S03 — продажи: привязка отзыва к лоту и нашей цели + хвосты ревью S01

**Цель.** docs/market/critique_sales_v1.md (раздел 3, «Решения архитектора», задача 3): каждая продажа (`market.pk_sales`)
привязывается к лоту и нашим целям через slug — так продажи конкурентов становятся данными «по товару».

**Прочитать.** docs/market/critique_sales_v1.md, docs/market/recon_testimonials.md (§4: `deal.item.slug == pk_lots.slug`,
`deal.item.id` — экземпляр сделки, не ключ), `core/market/sales.py`, `core/modules/market/pk_store.py` (pk_sales, pk_lots,
pk_labels, pk_verdicts), `core/modules/market/migrations/0006_pk_sales.sql`, `core/market/classify.py` (`prefilter`,
`label_fields`, `classify` — как размечается лот), `core/market/pk_client.py` (`card(slug)`), `tests/test_market_sales.py`.

**Можно менять.** `core/market/sales.py`, `core/modules/market/pk_store.py` (новые методы),
`core/modules/market/migrations/0010_pk_sale_link.sql` (новый; 0008/0009 заняты T16/T17), `tests/test_market_sales.py`,
`tests/test_market_pk_store.py`, `docs/market/spec.md` (раздел «Этап 2»).

**Интерфейс.**
1. `market.pk_sale_link(review_id PK → pk_sales, item_id NULL, product_id NULL, target_id NULL, link_kind TEXT NOT NULL
   CHECK in ('lot_match','card_fetch','foreign','ambiguous','pending'), linked_at)`. Для продажи может быть несколько целей
   одного паспорта → хранить по строке на цель: PK `(review_id, target_id)` с `target_id = ''` для foreign/pending.
2. `link_sales(store, client, llm, passports, limit)`:
   - slug есть в `pk_lots` → берём вердикты этого лота по нашим целям (MATCH) → `lot_match` (строка на каждую MATCH-цель;
     вердиктов MATCH нет → одна строка `foreign` с `item_id`);
   - у продавца несколько лотов одного товара (разные slug, одна цель MATCH) — продажа привязана к своему slug, но
     помечать `ambiguous`, если slug неизвестен, а у продавца ≥2 MATCH-лота этой цели;
   - slug неизвестен, `item_name` проходит `prefilter` какого-то нашего паспорта → `client.card(slug)` (≤ 40/мин,
     бюджет карточек за запуск — параметр) → `upsert_lots`/`set_card` → разметка/классификация как в пайплайне
     (переиспользовать функции classify, не копировать) → `card_fetch`; карточки нет (404) → `foreign`;
   - `item_name` не проходит ни один префильтр → `foreign` без запросов;
   - нет бюджета карточек/LLM → `pending` (следующий запуск).
3. Хвосты ревью S01: (а) скан, начатый с `scan_resume_cursor`, **не** поднимает водяной знак до `start_counter`, пока
   верх ленты (новее resume-курсора) не прочитан в этом же заходе; (б) `scan_seller` различает пустую страницу-флап и
   конец ленты (как `backfill_seller`); (в) исключение клиента → `last_scan_at` всё равно ставится (backoff работает),
   ошибка в `ScanResult.error`.
4. CLI: `python -m core.market.sales --link --db-dev [--cards 20]` — привязать непривязанные продажи.

**Приёмка (тесты без сети, фейковые клиент/LLM).**
- Продажа со slug лота с MATCH на 2 цели → 2 строки `lot_match`; лот без MATCH → `foreign`.
- Неизвестный slug, имя проходит префильтр → 1 вызов `card`, лот сохранён, вердикт посчитан, `card_fetch`.
- Имя не проходит префильтр → 0 запросов, `foreign`. Бюджет карточек 0 → `pending`.
- Хвосты S01 (а)–(в) — по тесту.
- `pytest -q tests/test_market_sales.py tests/test_market_pk_store.py tests/test_market_pk_client.py`

**Нельзя.** Менять `pipeline.py`, `classify.py`, `llm.py`, `crawl.py`, `worker/`; сеть в тестах; боевая БД; `bot.deps`.

**Сеть.** нет.

**Исполнитель.** muse.

**Коммит.** `feat(market): этап 2 — привязка продаж к лотам и целям (pk_sale_link) + хвосты S01`

## Решения арбитра (круг 3)

1. **HIGH — принято.** Карточка без `category_id` → лот не сохранять, ссылка `foreign` с `item_id = NULL` (никогда не
   писать `item_id`, которого нет в `pk_lots`); любая ошибка по одной продаже — лог + счётчик `LinkResult.errors`,
   заход продолжается. Тест на FK-сценарий.
2. **MEDIUM — принято.** Лот с финальными вердиктами текущей логики (MATCH/REJECT/UNSURE, отпечаток совпадает) — без
   догрузки карточки. Тест.
3. **MEDIUM — принято.** Кэш карточек и разметок на заход (slug → карточка/вердикты); повторная продажа того же slug в
   заходе — 0 запросов. Тест.
4. **MEDIUM — принято (важно).** Категории, создаваемые сборщиком, — `tracked = 0` (воркер их не обходит); явный
   параметр `upsert_category(..., tracked=0)`. Тест: после link_sales `list_due_categories` не содержит новую категорию.
5. Low — принять: лог исключения classify + счётчик; отпечаток для `set_card` и `classify` из одного и того же имени;
   комментарий миграции без несуществующего FK. Водяной знак resume/max_pages — оставить (закрыто решением S01 про
   крупных продавцов), только комментарий.
