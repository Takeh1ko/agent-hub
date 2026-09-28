"""Промпты конвейера: исполнитель, починка, ревьюер. Порт run_task.py."""

from __future__ import annotations

MAX_DIFF_CHARS = 200_000
MAX_OUTPUT_CHARS = 50_000

# Буквальный шаблон .agent/done.json в конце промпта исполнителя (≤ 15 строк).
DONE_TEMPLATE = """{
  "commit": "<sha HEAD>",
  "files": ["hub/x.py"],
  "tests": {"cmd": "python -m pytest -q", "ok": true, "tail": "хвост"},
  "notes": ".."
}"""

EXECUTOR_TAIL = (
    "В конце закоммить поимённо (`git add <пути>`, никогда -A) и запиши "
    ".agent/done.json по шаблону:\n"
    + DONE_TEMPLATE
    + "\nБез коммита и done.json работа не принята."
)


def executor_prompt(rules: str, card: str) -> str:
    """Промпт исполнителя: правила + карточка + шаблон done.json."""
    commit_line = ""
    for line in card.splitlines():
        s = line.strip()
        if s.startswith("**Коммит"):
            commit_line = s
            break
    tail = "Не пушь. Отчёт — в конце ответа."
    if commit_line:
        tail = f"Сообщение коммита:\n{commit_line}\nНе пушь. Отчёт — в конце ответа."
    return (
        f"{rules}\n\n---\nКАРТОЧКА ЗАДАЧИ:\n{card}\n\n---\n"
        "Работай в текущей директории (это твой worktree). "
        "Меняй только файлы из раздела «Можно менять». "
        f"{tail}\n{EXECUTOR_TAIL}"
    )


def fix_prompt(findings, gate) -> str:
    """Промпт доработки в сессию исполнителя: замечания + ворота."""
    if isinstance(gate, dict):
        tests = gate.get("tests", {}) if isinstance(gate.get("tests"), dict) else {}
        gate_line = (
            f"ворота: commit={gate.get('commit')}, "
            f"scope_violations={gate.get('scope')}, "
            f"tests_ok={tests.get('ok')}"
        )
        out = str(tests.get("output", ""))[:MAX_OUTPUT_CHARS]
    else:
        errs = getattr(gate, "errors", []) or []
        gate_line = f"ворота: {'; '.join(str(e) for e in errs) or 'красные'}"
        out = str(getattr(gate, "tests_tail", ""))[:MAX_OUTPUT_CHARS]
    import json as _json

    try:
        findings_text = _json.dumps(findings, ensure_ascii=False)
    except (TypeError, ValueError):
        findings_text = str(findings)
    return (
        "Ревьюер вернул замечания — исправь и закоммить новым коммитом "
        "(только файлы из «Можно менять», не пушь):\n"
        f"findings: {findings_text}\n"
        f"{gate_line}\n"
        f"вывод тестов:\n{out}"
    )


REVIEW_SCHEMA = (
    '{"verdict": "approve" | "changes" | "dispute", '
    '"findings": [{"severity": "high|medium|low", "file": ..., '
    '"line": ..., "issue": ..., "fix": ...}]}'
)


def review_prompt(rules: str, card: str, diff: str, gate, round: int,
                  blind: bool = False) -> str:
    """Промпт ревьюера в чистой сессии; при blind карточка без «Решений арбитра»."""
    if blind:
        try:
            from hub.gate.lint import strip_arbiter

            card = strip_arbiter(card)
        except ImportError:
            pass
    if isinstance(gate, dict):
        tests = gate.get("tests", {}) if isinstance(gate.get("tests"), dict) else {}
        gate_line = (
            f"РЕЗУЛЬТАТ ВОРОТ: commit={gate.get('commit')}, "
            f"scope_violations={gate.get('scope')}, "
            f"tests_ok={tests.get('ok')}\nвывод тестов:\n"
            f"{str(tests.get('output', ''))[-MAX_OUTPUT_CHARS:]}"
        )
    else:
        errs = getattr(gate, "errors", []) or []
        gate_line = (
            "РЕЗУЛЬТАТ ВОРОТ: "
            + ("зелёные" if getattr(gate, "ok", False) else "; ".join(str(e) for e in errs))
            + f"\nвывод тестов:\n{str(getattr(gate, 'tests_tail', ''))[-MAX_OUTPUT_CHARS:]}"
        )
    body = diff or "(пусто)"
    if len(body) > MAX_DIFF_CHARS:
        body = body[:MAX_DIFF_CHARS] + "\n…(дифф обрезан)"
    return (
        f"{rules}\n\n---\nКАРТОЧКА ЗАДАЧИ:\n{card}\n\n---\n"
        f"ДИФФ (ветка исполнителя против базы):\n{body}\n\n---\n"
        f"{gate_line}\n\n---\n"
        "Ты — ревьюер в свежей сессии, твоя задача — НАЙТИ ОШИБКИ, а не подтвердить работу. "
        "Прочитай всё из раздела «Прочитать» карточки и сверяй код с ним. "
        "Замечания severity high/medium → verdict changes. "
        f"Запиши вердикт JSON ровно по схеме {REVIEW_SCHEMA} "
        f"в файл `.agent/review_r{round}.json` (от корня worktree). "
        'verdict: approve (всё по карточке, ворота зелёные), '
        'changes (конкретные замечания в findings), '
        "dispute (карточка невыполнима/противоречива: укажи file:line и обоснование от 50 символов). "
        "Ничего не коммить."
    )
