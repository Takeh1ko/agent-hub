"""hub continue: новые «Решения арбитра» → та же ветка, база = merge-base."""

from __future__ import annotations

import subprocess
import time
import tomllib
from pathlib import Path

from hub.config import load_project
from hub.pipeline.common import clean_pycache, merge_base
from hub.read.git import is_dirty
from hub.store import Store

WIP_MSG = "wip: наработка до continue"
CARD_CHANGED_MSG = "карточка изменена → новая сессия исполнителя"


def _current_card_hash(task: dict, project) -> str | None:
    """sha256 текущего файла карточки, иначе None (нет файла)."""
    import hashlib as _hl

    rel = str(task.get("card_path") or "")
    if not rel:
        return None
    p = Path(rel)
    try:
        if p.is_absolute() and p.is_file():
            return _hl.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None
    cands: list[Path] = []
    try:
        root = str(getattr(project, "root", "") or "")
    except (AttributeError, ValueError):
        root = ""
    try:
        wt = str(task.get("worktree") or "")
    except (AttributeError, TypeError):
        wt = ""
    if root:
        cands.append(Path(root) / rel)
    if wt:
        cands.append(Path(wt) / rel)
    cands.append(Path(rel))
    for c in cands:
        try:
            if c.is_file():
                return _hl.sha256(c.read_bytes()).hexdigest()
        except OSError:
            continue
    return None


def _wip_commit(worktree: str) -> int:
    """Закоммитить грязную наработку одним wip-коммитом без `.agent*`.

    `.gitignore` проектов не трогаем — исключение делает сам
    (`git add -A -- . ':!.agent*'`). Чистое дерево — делать нечего (0).
    `__pycache__/.pytest_cache/*.pyc` чистим заранее, как конвейер, иначе
    wip захватит мусор pytest, а `is_dirty` увидит грязь там, где её нет.
    """
    try:
        clean_pycache(worktree)
    except (OSError, ValueError):
        pass
    try:
        dirty = bool(is_dirty(worktree))
    except (OSError, subprocess.SubprocessError):
        dirty = True
    if not dirty:
        return 0
    try:
        add = subprocess.run(["git", "add", "-A", "--", ".", ":!.agent*"],
                             cwd=worktree, capture_output=True,
                             text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"wip-add-fail: {e}")
        return 1
    if add.returncode != 0:
        print(f"wip-add-fail: {add.stderr.strip()[-500:] or add.returncode}")
        return 1
    try:
        staged = subprocess.run(["git", "diff", "--cached", "--quiet"],
                                cwd=worktree, capture_output=True,
                                text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        staged = None
    if staged is not None and staged.returncode == 0:
        return 0  # нечего коммитить (грязь была только в .agent*)
    try:
        com = subprocess.run(["git", "commit", "-m", WIP_MSG],
                             cwd=worktree, capture_output=True,
                             text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"wip-commit-fail: {e}")
        return 1
    if com.returncode != 0:
        tail = (com.stderr.strip() or com.stdout.strip())[-500:]
        print(f"wip-commit-fail: {tail or com.returncode}")
        return 1
    print(f"wip {worktree}: наработка закоммичена")
    return 0


def cmd_continue(args) -> int:
    task_id = args.task_id
    store = Store()
    task = store.get_task(task_id)
    if task is None:
        print(f"no-task: {task_id}")
        return 1
    proj_src = getattr(args, "project", None) or str(task.get("worktree") or ".")
    try:
        project = load_project(proj_src)
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError) as e:
        print(f"FAIL {task_id} no-project: {e}")
        return 1
    worktree = str(task.get("worktree") or "")
    branch = str(task.get("branch") or f"agent/{task_id}")
    root = str(getattr(project, "root", "") or "")
    if not worktree or not Path(worktree).is_dir():
        print(f"no-worktree: {worktree or '?'}")
        return 1
    if not root:
        print("no-project: нет root")
        return 1
    work_branch = (getattr(project, "work_branch", "") or "").strip()
    base_ref = work_branch or "HEAD"
    # Грязный worktree — сначала wip-коммит наработки (без .agent*),
    # иначе конвейер упадёт `failed: dirty`, а ручной коммит захватывает мусор.
    if _wip_commit(worktree):
        return 1
    # База = merge-base рабочей ветки и ветки задачи.
    base = merge_base(root, base_ref, branch)
    if not base:
        print(f"no-merge-base: {base_ref} {branch}")
        return 1
    # Старое .agent → .agent.prev_<ts>.
    agent = Path(worktree) / ".agent"
    if agent.exists():
        prev = Path(worktree) / f".agent.prev_{int(time.time())}"
        try:
            agent.rename(prev)
        except OSError as e:
            print(f"agent-rename-fail: {e}")
            return 1
    Path(worktree, ".agent").mkdir(parents=True, exist_ok=True)
    # H13 п.3: смена карточки → новая сессия исполнителя, иначе — прежняя.
    try:
        old_hash = str(task.get("card_hash") or "")
    except (AttributeError, TypeError):
        old_hash = ""
    try:
        new_hash = _current_card_hash(task, project)
    except (OSError, ValueError):
        new_hash = None
    card_changed = bool(new_hash and new_hash != old_hash)
    try:
        if card_changed and new_hash:
            store.upsert_task(id=task_id, base_sha=base, stage="queued",
                              round=0, stage_reason="continue: новые решения арбитра",
                              card_hash=new_hash)
        else:
            store.upsert_task(id=task_id, base_sha=base, stage="queued",
                              round=0, stage_reason="continue: новые решения арбитра")
        store.add_event(task_id, "stage", {"stage": "queued", "why": "continue",
                                           "base": base})
    except (OSError, ValueError) as e:
        print(f"store-fail: {e}")
        return 1
    if card_changed:
        try:
            store.add_event(task_id, "stage", {"reason": CARD_CHANGED_MSG})
        except (OSError, ValueError):
            pass
    # Флаг продолжения: HEAD ветки уже впереди базы (работа прошлого
    # исполнителя), штатный preflight (HEAD == base) к ней неприменим —
    # конвейер проверит merge-base вместо HEAD.
    try:
        from hub.pipeline.common import meta_del, meta_set

        meta_set(store, f"continued:{task_id}", "1")
        if card_changed:
            meta_set(store, f"exec_new_session:{task_id}", "1")
        else:
            # Прежняя сессия (--session старой): флаг новой сессии снять.
            try:
                meta_del(store, f"exec_new_session:{task_id}")
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass
    print(f"OK {task_id} base={base[:8]}")
    return 0


def register(subparsers) -> None:
    p = subparsers.add_parser("continue", help="продолжить задачу после решений арбитра")
    p.add_argument("task_id", help="ID задачи")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.set_defaults(func=cmd_continue)
