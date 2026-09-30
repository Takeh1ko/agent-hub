# agent-hub

Карта кода — читать первой, вместо изучения кода с нуля:
@docs/ARCHITECTURE.md

Согласованная архитектура — `docs/v2/architecture.md`, контракты — `docs/v2/contracts.md`, ход стройки и траты
Spark — `docs/v2/progress.md`. Старый хаб v1 — только история: `docs/v1/`.

После заметных изменений структуры (модуль, состояние, таблица, процесс) — обновить `docs/ARCHITECTURE.md`.

<!-- ahub:begin -->
## agent-hub
Задачи для моделей-работников — через `ahub` (навык `ahub`). В начале сессии — Monitor на `ahub watch`;
по строкам событий: `ahub status T<id>` → `ahub accept|rework|reject`. Сводка — `ahub status`.
<!-- ahub:end -->
