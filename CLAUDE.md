# agent-hub

Карта кода — читать первой, вместо изучения кода с нуля:
@docs/ARCHITECTURE.md

Архитектура — `docs/architecture.md`, контракты — `docs/contracts.md`. Текущая работа — выпуск на GitHub:
`docs/v3/plan.md`. История v1/v2 и личные заметки — вне репозитория: `~/Projects/Python/agent-hub-notes/`.

После заметных изменений структуры (модуль, состояние, таблица, процесс) — обновить `docs/ARCHITECTURE.md`.

<!-- ahub:begin -->
## agent-hub
Задачи для моделей-работников — через `ahub` (навык `ahub`). В начале сессии — Monitor на `ahub watch`;
по строкам событий: `ahub status T<id>` → `ahub accept|rework|reject`. Сводка — `ahub status`.
<!-- ahub:end -->
