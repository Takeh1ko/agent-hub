"""hub start: линт карточки → задача queued (идемпотентно)."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

from hub.config import load_project
from hub.gate.lint import lint_card
from hub.pipeline.common import (
    card_hash_of,
    current_base,
    parse_level,
    write_extra,
)
from hub.store import Store


_SECTION_NAMES = (
    "Решения арбитра",
    "Можно менять",
    "Интерфейс",
    "Прочитать",
    "Приёмка",
    "Исполнитель",
    "Уровень",
    "Коммит",
    "Цель",
    "Нельзя",
    "Сеть",
)


def _section_value(text: str, name: str) -> str:
    """Значение раздела карточки до следующего заголовка (свой разбор).

    Соседние секции на одной комбинированной строке
    (`**Сеть.** нет. **Уровень.** easy.`) не протекают друг в друга:
    режем по следующему заголовку, а не по границам строк lint.
    """
    import re

    names = sorted(_SECTION_NAMES, key=len, reverse=True)
    events: list[tuple[int, int, str, str]] = []  # (start, end, section, tail)

    def _match(inner: str) -> tuple[str, str] | None:
        s = inner.strip()
        for n in names:
            if s == n:
                return n, ""
            if s.startswith(n):
                after = s[len(n):]
                if after and after[0] in " .:()/*—-–":
                    return n, after[1:].strip()
        return None

    # Заголовки внутри `кода` (`sub/**`) — не заголовки: маскируем спаны
    # пробелами с сохранением позиций, иначе `**` из `sub/**` съедает
    # открывающее `**` следующей секции.
    masked = re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), text)
    for m in re.finditer(r"\*\*([^*]+?)\*\*", masked):
        hit = _match(m.group(1))
        if hit is not None:
            events.append((m.start(), m.end(), hit[0], hit[1]))
    for m in re.finditer(r"(?m)^(#{1,6})\s*(.+?)\s*$", masked):
        hit = _match(m.group(2))
        if hit is not None:
            tail = hit[1]
            # `# Уровень: easy` — значение внутри заголовка тоже забираем.
            events.append((m.start(), m.end(), hit[0], tail))
    events.sort(key=lambda e: e[0])
    for i, (st, en, sec, tail) in enumerate(events):
        if sec != name:
            continue
        nxt = events[i + 1][0] if i + 1 < len(events) else len(text)
        return (tail + " " + text[en:nxt]).strip()
    return ""


def _executor_for_card(text: str, project) -> str:
    """Исполнитель без --executor: Уровень → [levels], иначе Исполнитель."""
    import re as _re

    from hub.pipeline.runners import MODELS

    default = str(getattr(project.defaults, "executor", "") or "muse")
    lvl_sec = _section_value(text, "Уровень").lower()
    m = _re.search(r"(?<![\w])(easy|medium|hard)(?![\w])", lvl_sec)
    if m:
        lvl = m.group(1)
        levels = dict(getattr(project, "levels", None) or {})
        if lvl in levels and str(levels[lvl]).strip():
            return str(levels[lvl]).strip()
        builtin = {"easy": "gemini", "medium": "musefree", "hard": "muse"}
        return builtin.get(lvl, default)
    exec_sec = _section_value(text, "Исполнитель").lower()
    for name in sorted(MODELS, key=len, reverse=True):
        if not name:
            continue
        if _re.search(r"(?<![\w])" + _re.escape(name.lower()) + r"(?![\w])", exec_sec):
            return name
    return default


def _ensure_worktree(project, task_id: str, branch: str, base_sha: str,
                     worktrees_dir: str) -> tuple[str, str]:
    """Создать ветку + worktree от base_sha; существует — переиспользовать."""
    wt = str(Path(worktrees_dir) / task_id) if worktrees_dir else ""
    root = str(getattr(project, "root", "") or "")
    if not wt or not root:
        return "", ""
    if Path(wt).is_dir():
        return wt, branch
    Path(worktrees_dir).mkdir(parents=True, exist_ok=True)
    try:
        r = subprocess.run(["git", "worktree", "add", wt, "-b", branch, base_sha],
                           cwd=root, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return wt, branch
    if r.returncode != 0:
        # Ветка уже есть — привязать worktree к ней.
        try:
            r2 = subprocess.run(["git", "worktree", "add", wt, branch],
                                cwd=root, capture_output=True, text=True, timeout=120)
            if r2.returncode != 0:
                return "", ""
        except (OSError, subprocess.SubprocessError):
            return "", ""
    return wt, branch


def cmd_start(args) -> int:
    card = Path(args.card)
    if not card.is_file():
        print(f"{card}: нет карточки")
        return 1
    proj_src = getattr(args, "project", None) or str(card.parent if str(card.parent) else ".")
    try:
        project = load_project(proj_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"{card}: нет проекта: {e}")
        return 1
    res = lint_card(card, project)
    if not res.ok:
        for e in res.errors:
            print(e)
        return 1
    try:
        text = card.read_text(encoding="utf-8")
    except OSError as e:
        print(f"{card}: не читается: {e}")
        return 1
    chash = card_hash_of(card)
    root = str(getattr(project, "root", "") or "")
    base = current_base(project, root or None)
    if not base:
        print(f"{card}: нет базы (не git?)")
        return 1
    store = Store()
    # Идемпотентность: тот же card_hash+base_sha — тот же task.
    try:
        for t in store.list_tasks(active_only=False):
            if str(t.get("card_hash") or "") == chash and str(t.get("base_sha") or "") == base:
                print(f"OK {t['id']} (уже есть)")
                return 0
    except (OSError, ValueError):
        pass
    task_id = card.stem
    # Та же карточка, но база ушла вперёд: молча перетирать существующую
    # задачу нельзя на любом этапе (не только финальном) — иначе queued
    # падает с base-moved, stopped/failed тихо воскресает, а exec rN
    # у работающего воркера получает второй run_task на тот же worktree.
    # Дальше решает владелец: hub continue или новая карточка.
    try:
        old = store.get_task(task_id)
    except (OSError, ValueError):
        old = None
    if old is not None and str(old.get("card_hash") or "") == chash \
            and str(old.get("base_sha") or "") != base:
        print(f"уже есть {task_id} stage={old.get('stage')} "
              f"base={str(old.get('base_sha') or '')[:8]} ≠ {base[:8]}: "
              "дай hub continue или новую карточку")
        return 1
    explicit = (getattr(args, "executor", None) or "").strip()
    executor = explicit or _executor_for_card(text, project)
    reviewers = getattr(args, "reviewers", None)
    # Имена моделей — сразу: опечатка не должна молча исчезать в очереди.
    from hub.pipeline.review_levels import plan_for_card
    from hub.pipeline.runners import MODELS

    # Уровень ревью из карточки (**Ревью.** 1–4 / «свой»); флаги --reviewers/--rounds сильнее.
    plan, plan_err = plan_for_card(text, MODELS)
    if plan_err:
        print(f"{card}: {plan_err}")
        return 1
    if reviewers:
        rev_list = [r.strip() for r in str(reviewers).split(",") if r.strip()]
    elif plan is not None:
        rev_list = list(plan.reviewers)
    else:
        rev_list = list(project.defaults.reviewers or [])

    bad = [m for m in [executor, *rev_list] if m not in MODELS]
    if bad:
        for m in bad:
            print(f"неизвестная модель: {m}")
        return 1
    explicit_rounds = getattr(args, "rounds", None)
    if explicit_rounds:
        rounds = int(explicit_rounds)
    elif plan is not None:
        rounds = plan.rounds
    else:
        rounds = 2
    budget_go = getattr(args, "budget_go", None)
    try:
        budget_go_f = float(budget_go) if budget_go is not None else float(project.defaults.budget_go)
    except (TypeError, ValueError):
        budget_go_f = 0.5
    budget_usd = getattr(args, "budget_usd", None)
    try:
        budget_usd_f = (float(budget_usd) if budget_usd is not None
                        else float(getattr(project.defaults, "budget_usd", 0.0) or 0.0))
    except (TypeError, ValueError):
        budget_usd_f = 0.0
    after = (getattr(args, "after", None) or "").strip()
    blind = bool(getattr(args, "blind", False))
    level = parse_level(text)
    branch = f"agent/{task_id}"
    wt_dir = str(getattr(project, "worktrees", "") or "")
    worktree, _ = _ensure_worktree(project, task_id, branch, base, wt_dir)
    if not worktree:
        # Без worktree задачу всё равно заводим (preflight скажет dirty/no-worktree).
        worktree = str(Path(wt_dir) / task_id) if wt_dir else ""
    if worktree:
        # Переиспользованный worktree мог остаться с флагом stop_requested
        # от прошлого /stop (continue сносит его переименованием .agent,
        # start — нет): новый прогон мгновенно ушёл бы в stopped.
        try:
            (Path(worktree) / ".agent" / "stop_requested").unlink(missing_ok=True)
        except OSError:
            pass
    try:
        store.upsert_task(id=task_id, project=project.name, card_path=str(card),
                          card_hash=chash, level=level, branch=branch,
                          worktree=worktree, base_sha=base, stage="queued",
                          round=0, executor=executor,
                           reviewers_json=json.dumps(rev_list, ensure_ascii=False),
                           stage_reason="очередь", budget_go=budget_go_f,
                           budget_usd=budget_usd_f)
        write_extra(store, task_id, rounds, after, blind)
        store.add_event(task_id, "stage", {"stage": "queued", "card": str(card)})
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    review = plan.label if plan is not None and not reviewers and not explicit_rounds else "вручную"
    if plan is None and not reviewers and not explicit_rounds:
        review = "по умолчанию проекта"
    print(f"OK {task_id} · ревью: {review} — {', '.join(rev_list) or 'нет'}, кругов {rounds}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("start", help="поставить карточку в очередь")
    p.add_argument("card", help="путь к карточке .md")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.add_argument("--executor", default=None, help="исполнитель (короткое имя)")
    p.add_argument("--reviewers", default=None, help="ревьюеры через запятую")
    p.add_argument("--rounds", type=int, default=None,
                   help="кругов ревью (по умолчанию — из «Ревью» карточки, иначе 2)")
    p.add_argument("--budget-go", type=float, default=None, help="бюджет Go $")
    p.add_argument("--budget-usd", type=float, default=None,
                   help="бюджет реальных $ (0 — запрет трат)")
    p.add_argument("--after", default=None, help="ждать задачу ID")
    p.add_argument("--blind", action="store_true", help="слепое ревью без эталона")
    p.set_defaults(func=cmd_start)
