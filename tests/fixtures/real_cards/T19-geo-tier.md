# T19 — гео-ярус: конкуренты только из того же ценового яруса по региону (РФ отдельно)

**Цель.** Решение владельца (2026-09-28): сравниваем игры по гео. Ключи с РФ обычно намного дороже, поэтому если наш товар
**не** для РФ, лоты «с РФ» ему не конкуренты, и наоборот. Отменяет прежнее правило «надмножество региона = конкурент»
(T06g/T16, `_broad_region_covers`, комментарий про GLOBAL).

**Правило (выполнить буквально).**
- «С РФ» у лота: в регионах есть RU, или GLOBAL/«весь мир» без исключения RU. «Без РФ»: RU исключён или регионы явно
  без RU (CIS без RU, KZ, UA, EU, TR…).
- Цель с РФ (`region_target` ∋ RU или GLOBAL, RU не в исключениях) → конкурент только лот «с РФ».
- Цель без РФ (RU в `region_exclusions` или `region_target` без RU) → конкурент только лот «без РФ».
- Так же для РБ, **если** цель явно исключает BY: лот, работающий в BY, — не конкурент; если цель BY не упоминает — BY не
  влияет.
- Всё прочее (регион лота должен покрывать регион цели) — как сейчас.

**Прочитать.** `core/market/classify.py` (`_broad_region_covers`, `_lot_excluded_by_target`, `_market_outcome`, как
разметка `regions/region_exclusions` → решение), `core/sourcing/v2/rules.py` (`decide` — только читать), тесты
`tests/test_market_classify.py` (случаи регионов, T06g «СНГ без РФ vs RU+CIS», GLOBAL), `tests/fixtures/market/gold_t08.json`.

**Можно менять.** `core/market/classify.py`, `tests/test_market_classify.py`, `tests/fixtures/market/gold_t08.json`
(только поле `gold` у пар, которые меняются по новому правилу, с `gold_source="owner_geo_rule"`), `core/market/eval_gold.py`
(только если нужно), `docs/market/spec.md` (правило гео-яруса), `docs/market/decision_verdict_trust.md` (пометка).

**Интерфейс.** Функция `geo_tier(regions, exclusions) -> "ru" | "no_ru" | "unknown"` и проверка яруса цели vs лота в
`_market_outcome` (после остальных правил). `unknown` у лота → UNSURE (`needs:region`), не MATCH. `LOGIC_PK` → `pk.v8`
(вердикты пересчитаются по сохранённой разметке без LLM — разметку не трогать, `LOGIC_LABEL` не менять).

**Приёмка.**
- Цель «СНГ без РФ/РБ» против лота RU+CIS → REJECT (`geo:ru_vs_no_ru`); против GLOBAL → REJECT; против «СНГ без РФ» → MATCH;
  против «КЗ» → MATCH.
- Цель «РФ» против RU+TR+EU → MATCH; против «СНГ без РФ» → REJECT; против GLOBAL → MATCH.
- Цель «РФ+СНГ» против RU → MATCH (оба «с РФ»).
- Лот без региона ни в заголовке, ни в разметке → UNSURE.
- gold_t08: пары, где новое правило меняет ответ, обновлены (список в отчёте); `eval_gold` на фейковой модели зелёный.
- `pytest -q tests/test_market_classify.py tests/test_market_pipeline.py tests/test_market_eval_gold.py tests/test_market_slice.py`

**Нельзя.** Менять разметку/промпт (`LLM_PROMPT`, `label_fields`), `llm.py`, `pipeline.py`, `crawl.py`; сеть; боевая БД.

**Сеть.** нет.

**Исполнитель.** muse.

**Коммит.** `feat(market): гео-ярус — конкуренты только из того же ценового яруса по региону (pk.v8)`
