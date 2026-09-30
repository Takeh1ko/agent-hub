"""Промпты работникам: минимальные, по контракту итога (contracts §2). Правила проекта — первыми."""

from __future__ import annotations

from ahub.config import ProjectConfig
from ahub.model import Kind
from ahub.store import Task

DEFAULT_RULES = """# Правила работника agent-hub
- Ты в отдельной копии репозитория (git worktree). Выходить за её пределы нельзя.
- Настоящие данные, чужие базы, секреты (.env, ключи, /etc) не трогать; сеть — только если задача прямо требует.
- Служебный каталог хаба — `.ahub/` (в git не попадает): туда пишешь итог и отчёт.
- Интерфейсы и имена из задачи — контракт: не переименовывать.
"""

REPORT_LIMIT_KB = 12


def rules_text(project: ProjectConfig) -> str:
    p = project.rules_path()
    if p is not None and p.is_file():
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            pass
    return DEFAULT_RULES


def _header(task: Task) -> str:
    kind = {Kind.SCOUT: "разведка", Kind.CODE: "код", Kind.REVIEW: "ревью", Kind.ROUTINE: "рутина"}[task.kind]
    parts = [f"# Задача {task.label} ({kind}): {task.title}"]
    if task.spec.strip():
        parts.append(task.spec.strip())
    read = task.limits.get("read") or []
    if read:
        parts.append("## Прочитать сначала\n" + "\n".join(f"- `{p}`" for p in read))
    if task.result_format.strip():
        parts.append("## Какой нужен результат\n" + task.result_format.strip())
    return "\n\n".join(parts)


SCOUT_DELIVERY = f"""## Как сдать (обязательно; главнее общих правил проекта о коммитах и отчётах)
1. В проекте ничего не меняй и не коммить — это разведка. Создавать файлы можно только в `.ahub/`.
2. Отчёт — `.ahub/report.md` (≤ {REPORT_LIMIT_KB} КБ). Первый раздел — `## Суть`: не больше 10 строк, главное, что
   нужно знать, чтобы принять решение. Дальше — подробности с путями `файл:строка`.
3. Итог — `.ahub/result.json`:
   {{"summary": "1–3 предложения", "status": "done", "questions": ["что осталось неясным"], "notes": "что не проверил"}}
   Если продолжать нельзя (нет доступа, противоречие в задаче) — "status": "blocked" и причина в summary.
4. Последнее сообщение — одна строка: «готово» или «заблокировано: причина».
"""


def scout_prompt(project: ProjectConfig, task: Task) -> str:
    return "\n\n".join([rules_text(project).strip(), _header(task), SCOUT_DELIVERY])


def repair_prompt(problem: str) -> str:
    return (f"Итог не сдан по форме: {problem}.\n"
            "Ничего нового не исследуй. Допиши недостающее строго по разделу «Как сдать» из задачи "
            "(`.ahub/result.json`, для разведки ещё `.ahub/report.md`) и ответь одной строкой «готово».")


CONTINUE_PROMPT = ("Сессия прервалась (тишина или сбой). Продолжи задачу с того места, где остановился; "
                   "если всё уже сделано — сдай итог по разделу «Как сдать».")

STOP_PROMPT = ("Хаб просит остановиться (бюджет или команда). Ничего нового не начинай: сохрани сделанное "
               "(для кода — закоммить поимённо), запиши `.ahub/result.json` со статусом того, что готово, и ответь «готово».")
