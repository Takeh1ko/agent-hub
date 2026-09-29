# T18b — хвосты T18 (два потока): тест реального обрыва прокси, приоритет CLI, дедуп, стабильные тайминги

**Цель.** Закрыть замечания последнего ревью T18 (слит арбитром с хвостами).

**Прочитать.** `worker/market_pk.py` (`run_cycle`, `run_cycle_parallel`, `ensure_proxies`, `run`, `maybe_heartbeat`),
`core/market/pipeline.py` (`SliceReport.proxy_down`, `run_category`: места установки флага), `core/market/crawl.py`
(`full_pass`: как глотается ProxyDown), docs/market/spec.md §4.7, `tests/test_market_worker.py`.

**Можно менять.** `worker/market_pk.py`, `core/market/pipeline.py` (только: после обрыва каталога с `proxy_down`
пропускать фазу карточек), `tests/test_market_worker.py`, `tests/test_market_pipeline.py` (только пороги
тайминг-тестов), `docs/market/spec.md` (§4.7).

**Интерфейс / что сделать.**
1. **MEDIUM.** Тест прод-пути `proxy_down` в режиме full: настоящий `crawl.full_pass` с клиентом, бросающим ProxyDown
   (он сам глотает и возвращает `aborted=True` с текстом ошибки) → `rep.proxy_down is True`.
2. После обрыва каталога (`proxy_down`) фаза карточек не стартует (сразу `cards_stop`) — без лишних ~180 с сети.
3. Явный `--proxy/--proxies` из CLI перебивает `MARKET_PK_PROXIES`; `--proxies` дедуплицируется как env-путь.
4. Число потоков ≤ размера пула БД (`min(len(clients), pool max_size)`), в spec — одной фразой.
5. `run_cycle` (однопоточный) — docstring «legacy, прод не зовёт» и ветка aborted с `rep.proxy_down` — как в
   параллельном (сон без `note_category_error`).
6. spec §4.7: «не больше 3 обрывов подряд на категорию (PROXY_DOWN_MAX), на третьем — как обычная ошибка».
7. Тест: разбивка по потокам попадает в лог heartbeat (строка с `pid:proxy-`).
8. Тайминг-тесты (`test_two_threads_each_once_and_parallel`, `PipelineStreamTest.test_cards_and_llm_overlap`) —
   мерить ускорение относительно последовательного прогона, а не абсолютные секунды (под нагрузкой не флакают).

**Приёмка.** `pytest -q tests/test_market_worker.py tests/test_market_pk_store.py tests/test_market_pipeline.py`

**Нельзя.** `crawl.py`, `pk_client.py`, `classify.py`, `llm.py`; сеть.

**Сеть.** нет. **Исполнитель.** musefree.

**Коммит.** `fix(market): хвосты T18 — реальный обрыв прокси, приоритет CLI, дедуп, стабильные тайминги`
