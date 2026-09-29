# T14b — честный суточный бюджет разметки и кран трат DeepSeek

**Цель.** docs/market/critique_v2.md п. 1 и блокер 2: `Budget(llm_budget_day)` создаётся на каждый проход (`pipeline.py`) —
«суточный» лимит стал часовым; платная добивка не имеет крана трат. Плюс интеграционные тесты инвалидации T16 (ревью).

**Прочитать.** docs/market/critique_v2.md, docs/market/decision_verdict_trust.md, `core/market/pipeline.py` (`Budget`, этап
разметки, `_verdict_reusable`, `PkLabels`), `core/market/classify.py` (`Budget`), `core/market/llm.py` (`VerdictLLM`:
`budget_per_day`, `used_today`, `paid_fallback`, `stats()` стоимость DeepSeek), `core/modules/market/pk_store.py`
(`pk_categories.llm_budget_day`), migrations.

**Можно менять.** `core/market/pipeline.py`, `core/market/classify.py` (только `Budget`), `core/market/llm.py`,
`core/modules/market/pk_store.py` (новые методы), `core/modules/market/migrations/0011_pk_llm_budget.sql` (новый;
0009/0010 заняты T17/S03), `tests/test_market_pipeline.py`, `tests/test_market_llm.py`, `tests/test_market_pk_store.py`,
`docs/market/spec.md`.

**Интерфейс.**
1. **Суточный бюджет категории в БД:** `market.pk_llm_usage(category_id, day DATE, calls INT, paid_calls INT,
   paid_usd NUMERIC, PK (category_id, day))`; `day` — сутки ЕКБ (как `category_daily`). Проход берёт остаток
   `llm_budget_day − calls` за сегодня и списывает атомарно (`UPDATE … SET calls = calls + 1 WHERE … AND calls < limit
   RETURNING`), а не создаёт новый `Budget` на проход. Параллельные проходы одной категории не превышают лимит.
2. **Кран DeepSeek:** `VerdictLLM(paid_usd_per_day=0.5)` — при достижении суммы за сутки (UTC) платные вызовы
   не делаются (`paid_fallback` фактически выключается до конца суток), `stats()['deepseek']['usd_today']`.
   Стоимость — по `usage` (0.15/0.6/0.003 за 1M). Параметр задаёт воркер.
3. **Провал `label_many` не списывает бюджет** (ни `used_today`, ни `pk_llm_usage`) — списывается только принятый
   провайдером запрос с ответом (или по факту HTTP-вызова — выбрать и описать в spec, но единообразно).
4. **Интеграционные тесты T16 через `run_category`** (без моков внутренностей): (а) в `pk_labels` лежит устаревшая по
   `label_is_stale` разметка `lbl.v1` → проход вызывает LLM заново; (б) вердикт в `pk_verdicts` от такой разметки
   не переиспользуется; (в) платная MATCH-разметка `needs_free` → этап (а) переразмечает бесплатной.
5. **Два платных слота по очереди (решение владельца 2026-09-28).** Сначала выжимаются все бесплатные слоты. Платные:
   (1) `deepseek` — официальный API, на балансе ~$0,70: HTTP 402 / «Insufficient Balance» → слот помечается
   `exhausted` навсегда (запись в БД, не только в памяти процесса) и больше не вызывается; (2) `deepseek-oc` —
   OpenCode (OpenAI-совместимый `https://opencode.ai/zen/v1/chat/completions`, модель `deepseek-v4.1-flash`, URL и модель —
   константы; ключ — `load_opencode_key()` из `core/sourcing/v2/llm/keys.py` (ключ зашит в `embedded_keys.py` —
   решение владельца; нет ключа → слот выключен), цена $0,30 / $1,20 / $0,006 за 1M. **Общий потолок за всё время** `paid_usd_total_cap` (по
   умолчанию 3.0) на каждый платный слот: накопленная сумма — `market.pk_llm_spend(provider TEXT PRIMARY KEY, usd NUMERIC,
   exhausted BOOL, updated_at)`, списание атомарно; при достижении — слот выключен навсегда (до ручного сброса строки).
   Жёсткий предохранитель: сумма всех платных слотов > 5.0 → платные выключены полностью.

**Приёмка.**
- Три прохода за одни сутки при `llm_budget_day=10` и 30 кандидатах → суммарно ровно 10 вызовов; новые сутки → снова 10.
- Два параллельных прохода одной категории (потоки) → ≤ 10 вызовов суммарно.
- Кран: при `paid_usd_per_day=0.001` после первого платного вызова дальше платных нет; бесплатные работают.
- Сбой пачки `label_many` → счётчики не выросли.
- Тесты T16 (а)–(в).
- 402 от `deepseek` → следующий вызов идёт в `deepseek-oc`; новый `VerdictLLM` (рестарт) тоже не зовёт `deepseek`.
- `deepseek-oc` при накопленных $2,999 и вызове на $0,002 → вызов засчитан, следующий не делается; потолок 5.0 общий.
- `pytest -q tests/test_market_pipeline.py tests/test_market_llm.py tests/test_market_pk_store.py tests/test_market_classify.py tests/test_market_worker.py`

**Нельзя.** Менять `worker/market_pk.py` (его меняет T17 — параметр крана воркер передаст позже), `sales.py`, `crawl.py`,
`rank.py`, `read.py`; сеть; боевая БД.

**Сеть.** нет.

**Исполнитель.** muse. **Ревью.** mimo (деньги).

**Коммит.** `feat(market): суточный бюджет разметки в БД и кран трат DeepSeek; интеграционные тесты инвалидации T16`

## Решения арбитра (круг 3)

Принять **все** замечания ревью круга 2 (`.agent.prev_*/review_r2.json`), с тестами:
1. **MEDIUM.** Дневной кран не считает дважды: повторная `bind_spend_store` (новый PkStore на каждый проход) не
   складывает восстановленную из БД сумму с `slot.cost_today` этого же процесса. Тест: два прохода подряд с платной
   тратой $0,10 → `_paid_today_total() == 0.10`, не 0.20.
2. **MEDIUM.** Платный слот занят (`paid_busy`/`in_flight > 0`) → задачи **ждут** его (has_viable_slot, малое ожидание),
   а не падают «нет слотов», особенно когда бесплатные упёрлись в дневной 429. Тест: бесплатные 429, платный занят →
   вторая задача дождалась и получила ответ.
3. Low: `llm_budget_day = 0` выключает LLM категории (не превращается в 200); кран после рестарта — из большего из
   двух источников или ограничение явно в spec §4.3; предохранитель $5 виден в `stats()` отдельным флагом/причиной;
   **во всех тестах** `deepseek_oc_key=""` (боевой ключ OpenCode не должен попадать в тесты); удалить мёртвое
   `_spend_bound` и недостижимую ветку `stats()`.
