"""Слияние готовой задачи и чистка осиротевших worktree/веток."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path


def _run_git(cwd: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["git", *args], cwd=cwd,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        err = e.stderr if isinstance(e.stderr, str) else ""
        return subprocess.CompletedProcess(args=["git", *args], returncode=124,
                                           stdout="", stderr=f"timeout: {err}")
    except OSError as e:
        return subprocess.CompletedProcess(args=["git", *args], returncode=127,
                                           stdout="", stderr=str(e))


def _run_acceptance(root: str, py: str, lock: str | None) -> tuple[bool, str]:
    """Приёмка на слитом дереве: pytest -q под замком, без проверки диффа/грязи."""
    import fcntl
    import os as _os

    lock_fd = None
    if lock:
        try:
            Path(lock).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            lock_fd = _os.open(str(lock), _os.O_CREAT | _os.O_RDWR, 0o644)
        except OSError as e:
            return False, f"lock-error: {e}"
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            try:
                from hub.read.procs import lock_holder as _lh

                holder = _lh(str(lock))
                msg = f"locked: pid {holder.pid}" if holder is not None else "locked: pid ?"
            except (OSError, ValueError):
                msg = "locked: pid ?"
            _os.close(lock_fd)
            return False, msg
    try:
        try:
            r = subprocess.run([py, "-m", "pytest", "-q"], cwd=root,
                               capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else ""
            err = e.stderr if isinstance(e.stderr, str) else ""
            tail = ((out + "\n" + err) if out and err else (out + err)).strip()[-2000:]
            return False, f"tests-fail: timeout: {tail}" if tail else "tests-fail: timeout"
        except OSError as e:
            return False, f"tests-fail: не запустилась: {e}"
        out, err = r.stdout or "", r.stderr or ""
        tail = ((out + "\n" + err) if out and err else (out + err)).strip()[-2000:]
        if r.returncode != 0:
            return False, f"tests-fail: {tail}" if tail else f"tests-fail: код {r.returncode}"
        return True, tail
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            _os.close(lock_fd)


def merge_task(store, project, task_id: str, force: bool = False) -> tuple[bool, str]:
    """Слить ветку задачи в work_branch. Возврат (ok, сообщение).

    Только из ready (или --force из arbiter): перепроверка ворот по git,
    merge --no-ff, приёмка, откат при красных, push по конфигу,
    хук task_cleanup, удаление worktree/ветки. Конфликт → abort, этап не меняется.
    """
    from hub.gate.gate import check_gate
    from hub.gate.donefile import load_done
    from hub.gate.preflight import run_hook

    task = store.get_task(task_id)
    if task is None:
        return False, f"no-task: {task_id}"
    stage = str(task.get("stage") or "")
    if stage != "ready" and not (force and stage == "arbiter"):
        return False, f"not-ready: {stage or '?'}"
    worktree = str(task.get("worktree") or "")
    base_sha = str(task.get("base_sha") or "")
    branch = str(task.get("branch") or f"agent/{task_id}")
    if not worktree or not Path(worktree).is_dir():
        return False, f"no-worktree: {worktree or '?'}"
    if not base_sha:
        return False, "no-base: пустой base_sha задачи"
    root = str(getattr(project, "root", "") or "")
    if not root or not Path(root).is_dir():
        return False, "no-project: нет root проекта"
    work_branch = (getattr(project, "work_branch", "") or "").strip()
    if not work_branch:
        # Дефолт — текущая ветка основного репо (Н8).
        r = _run_git(root, "branch", "--show-current")
        work_branch = r.stdout.strip() if r.returncode == 0 else ""
        if not work_branch:
            return False, "no-work-branch: пустой work_branch и нет текущей ветки"

    # Перепроверка ворот по git: done.json (+ mismatch, files ⊆ diff) + check_gate.
    try:
        done = load_done(Path(worktree))
    except FileNotFoundError as e:
        return False, str(e)
    except ValueError as e:
        return False, str(e)
    head = _run_git(worktree, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        return False, "no-head: git rev-parse HEAD не сработал"
    head_sha = head.stdout.strip()
    if done.commit != head_sha:
        return False, f"mismatch: done={done.commit} head={head_sha}"
    try:
        from hub.pipeline.cycle import _resolve_card
        from hub.pipeline.common import card_globs as _cg

        card_p = _resolve_card(task, project)
        card_text = card_p.read_text(encoding="utf-8") if card_p is not None else ""
        globs = _cg(card_text) if card_text else []
    except (OSError, ValueError):
        card_text = ""
        globs = []
    import fnmatch as _fn

    proj_allowed = list(getattr(project, "allowed_paths", []) or [])
    allowed = [g for g in globs
               if not (Path(g).is_absolute() or ".." in Path(g).parts)
               and any(_fn.fnmatch(g.removeprefix("./"), pat) for pat in proj_allowed)]
    # Сверка done.files ⊆ diff (как hub gate): лживый done.json не проходит.
    _dr = _run_git(worktree, "-c", "core.quotepath=false", "diff", "--no-renames",
                   "--name-only", f"{base_sha}..{head_sha}", "--")
    if _dr.returncode != 0:
        return False, "no-diff: ворота не проверены"
    _diff_set = {l for l in (s.strip() for s in _dr.stdout.splitlines()) if l}
    for f in done.files:
        if f not in _diff_set:
            return False, f"unknown-file: {f}"
    py = (getattr(project, "python", "") or "").strip() or sys.executable
    lock = (getattr(project, "test_lock", "") or "").strip() or None
    from hub.pipeline.common import clean_pycache as _clean

    _clean(worktree)
    from hub.gate.acceptance import acceptance_cmd

    gate = check_gate(Path(worktree), base_sha, head_sha, allowed,
                      acceptance_cmd(card_text, py), lock)
    if not gate.ok:
        try:
            store.add_event(task_id, "stage", {"stage": stage, "merge": "gate-red",
                                               "errors": gate.errors})
        except (OSError, sqlite3.Error, ValueError):
            pass
        return False, "; ".join(gate.errors)[:500] or "ворота красные"

    # Слияние строго в work_branch: проверяем checkout, иначе работа
    # ляжет в чужую ветку, а push уйдёт без неё.
    cur = _run_git(root, "branch", "--show-current")
    cur_branch = cur.stdout.strip() if cur.returncode == 0 else ""
    if cur_branch != work_branch:
        return False, (f"not-on-work-branch: HEAD={cur_branch or '?'} "
                       f"({root}), нужен {work_branch}")
    # Слияние в work_branch основного репо: сразу с коммитом,
    # приёмка — уже на слитом HEAD, откат при красных.
    pre = _run_git(root, "rev-parse", "HEAD")
    pre_sha = pre.stdout.strip() if pre.returncode == 0 else ""
    mg = _run_git(root, "merge", "--no-ff", "-m",
                  f"Merge {branch} в {work_branch} ({task_id})", branch)
    if mg.returncode != 0:
        _run_git(root, "merge", "--abort")
        try:
            store.add_event(task_id, "stage", {"stage": stage, "merge": "conflict"})
        except (OSError, sqlite3.Error, ValueError):
            pass
        tail = (mg.stdout + "\n" + mg.stderr).strip().splitlines()
        one = tail[-1] if tail else "конфликт"
        return False, f"conflict: {one}"[:500]
    merged = _run_git(root, "rev-parse", "HEAD")
    merged_sha = merged.stdout.strip() if merged.returncode == 0 else ""
    # Приёмка на слитом дереве (только тесты, без проверки диффа/грязи root).
    acc_ok, acc_msg = _run_acceptance(root, py, lock)
    # pytest в корне оставляет .pytest_cache/__pycache__ — чистим, как worktree
    # перед воротами, иначе каждое слияние грязнит work_branch.
    _clean(root)
    if not acc_ok and acc_msg.startswith("locked:"):
        if pre_sha:
            _run_git(root, "reset", "--hard", pre_sha)
        return False, acc_msg[:500]
    if not acc_ok:
        if pre_sha:
            _run_git(root, "reset", "--hard", pre_sha)
        try:
            store.add_event(task_id, "stage", {"stage": stage, "merge": "rollback-tests"})
        except (OSError, sqlite3.Error, ValueError):
            pass
        return False, acc_msg[:500]
    # Push по конфигу (пусто — не пушить).
    push = (getattr(project, "push", "") or "").strip()
    if push:
        pr = _run_git(root, "push", *push.split())
        if pr.returncode != 0:
            # Слияние уже в локальной ветке: откатываем к pre, этап не меняем.
            if pre_sha:
                _run_git(root, "reset", "--hard", pre_sha)
            tail = (pr.stdout + "\n" + pr.stderr).strip().splitlines()
            one = tail[-1] if tail else "push не удался"
            try:
                store.add_event(task_id, "stage", {"stage": stage, "merge": "push-fail"})
            except (OSError, sqlite3.Error, ValueError):
                pass
            return False, f"push-fail: {one}"[:500]
    # Хук task_cleanup (best-effort, не валит слияние).
    hook = ""
    try:
        hook = (project.hooks.task_cleanup or "").strip() if project.hooks else ""
    except AttributeError:
        hook = ""
    if hook:
        env = {"HUB_TASK_ID": task_id, "HUB_WORKTREE": worktree,
               "HUB_PROJECT_ROOT": root}
        try:
            run_hook(hook, env, Path(root))
        except (OSError, subprocess.SubprocessError):
            pass
    # Чистка: worktree + ветка.
    _run_git(root, "worktree", "remove", "--force", worktree)
    _run_git(root, "branch", "-D", branch)
    try:
        store.upsert_task(id=task_id, stage="merged", merged_sha=merged_sha,
                          stage_reason=f"слито в {work_branch}")
        store.add_event(task_id, "stage", {"stage": "merged", "sha": merged_sha})
    except (OSError, sqlite3.Error, ValueError):
        pass
    return True, f"OK {task_id} {merged_sha}"


def list_orphans(project, store) -> tuple[list[dict], list[str]]:
    """Осиротевшие worktree/ветки без задачи в store.

    Возврат (worktrees, branches): worktrees — записи git worktree list,
    branches — ветки agent/* без задачи.
    """
    from hub.read.git import worktrees as _wts

    root = str(getattr(project, "root", "") or "")
    if not root:
        return [], []
    wts = _wts(root)
    try:
        tasks = store.list_tasks(active_only=False)
    except (OSError, ValueError):
        tasks = []
    known_wt = {str(t.get("worktree") or "") for t in tasks}
    known_br = {str(t.get("branch") or "") for t in tasks}
    orph_wt = [w for w in wts if str(w.get("path") or "") not in known_wt
               and str(w.get("path") or "") != root]
    r = _run_git(root, "branch", "--list", "agent/*")
    branches = []
    if r.returncode == 0:
        for b in r.stdout.splitlines():
            # Ветка, checkout-нутая в worktree, помечена '+' («+ agent/orph»,
            # «*+ ...»); срезаем оба префикса, иначе `branch -D "+ agent/orph"`
            # молча падает, а clean печатает OK.
            s = b.strip().lstrip("*+").strip()
            if not s:
                continue
            branches.append(s)
    orph_br = [b for b in branches if b not in known_br]
    return orph_wt, orph_br
