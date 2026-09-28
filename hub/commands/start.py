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
    executor = getattr(args, "executor", None) or project.defaults.executor
    reviewers = getattr(args, "reviewers", None)
    if reviewers:
        rev_list = [r.strip() for r in str(reviewers).split(",") if r.strip()]
    else:
        rev_list = list(project.defaults.reviewers or [])
    # Имена моделей — сразу: опечатка не должна молча исчезать в очереди.
    from hub.pipeline.runners import MODELS

    bad = [m for m in [executor, *rev_list] if m not in MODELS]
    if bad:
        for m in bad:
            print(f"неизвестная модель: {m}")
        return 1
    rounds = int(getattr(args, "rounds", 2) or 2)
    budget_go = getattr(args, "budget_go", None)
    try:
        budget_go_f = float(budget_go) if budget_go is not None else float(project.defaults.budget_go)
    except (TypeError, ValueError):
        budget_go_f = 0.5
    after = (getattr(args, "after", None) or "").strip()
    blind = bool(getattr(args, "blind", False))
    level = parse_level(text)
    branch = f"agent/{task_id}"
    wt_dir = str(getattr(project, "worktrees", "") or "")
    worktree, _ = _ensure_worktree(project, task_id, branch, base, wt_dir)
    if not worktree:
        # Без worktree задачу всё равно заводим (preflight скажет dirty/no-worktree).
        worktree = str(Path(wt_dir) / task_id) if wt_dir else ""
    try:
        store.upsert_task(id=task_id, project=project.name, card_path=str(card),
                          card_hash=chash, level=level, branch=branch,
                          worktree=worktree, base_sha=base, stage="queued",
                          round=0, executor=executor,
                          reviewers_json=json.dumps(rev_list, ensure_ascii=False),
                          stage_reason="очередь", budget_go=budget_go_f,
                          budget_usd=0.0)
        write_extra(store, task_id, rounds, after, blind)
        store.add_event(task_id, "stage", {"stage": "queued", "card": str(card)})
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    print(f"OK {task_id}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("start", help="поставить карточку в очередь")
    p.add_argument("card", help="путь к карточке .md")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.add_argument("--executor", default=None, help="исполнитель (короткое имя)")
    p.add_argument("--reviewers", default=None, help="ревьюеры через запятую")
    p.add_argument("--rounds", type=int, default=2, help="кругов ревью")
    p.add_argument("--budget-go", type=float, default=None, help="бюджет Go $")
    p.add_argument("--after", default=None, help="ждать задачу ID")
    p.add_argument("--blind", action="store_true", help="слепое ревью без эталона")
    p.set_defaults(func=cmd_start)
