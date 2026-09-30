"""hub review: только ворота + панель ревью (без исполнителя)."""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tomllib
from pathlib import Path

from hub.config import load_project
from hub.pipeline import prompts
from hub.pipeline.common import read_extra
from hub.pipeline.cycle import (
    _allowed_intersection,
    _card_globs,
    _collect_reviews,
    _diff_files,
    _diff_text,
    _read_rules,
    _resolve_card,
    _review_file_valid,
    _runner_tool_model,
    _set_stage,
    _unlink_round_reviews,
    REVIEW_FIX_TEXT,
)
from hub.store import Store


def cmd_review(args) -> int:
    task_id = args.task_id
    blind = bool(getattr(args, "blind", False))
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
    base_sha = str(task.get("base_sha") or "")
    if not worktree or not Path(worktree).is_dir() or not base_sha:
        print(f"no-worktree/base: {worktree or '?'}")
        return 1
    rounds, _, task_blind = read_extra(task)
    blind = blind or task_blind
    card_path = _resolve_card(task, project)
    if card_path is None:
        print(f"no-card: {task.get('card_path') or '?'}")
        return 1
    try:
        card_text = card_path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"no-card: {e}")
        return 1
    rules_text = _read_rules(project)
    allowed = _allowed_intersection(_card_globs(card_text),
                                    list(getattr(project, "allowed_paths", []) or []))
    # Ворота: done.json + check_gate (как в cycle, но без repair).
    from hub.gate.donefile import load_done
    from hub.gate.gate import check_gate
    import subprocess as _sp
    import sys as _sys

    try:
        done = load_done(Path(worktree))
    except FileNotFoundError as e:
        print(str(e))
        return 1
    except ValueError as e:
        print(str(e))
        return 1
    try:
        r = _sp.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                    capture_output=True, text=True, timeout=60)
    except OSError as e:
        print(f"no-head: {e}")
        return 1
    head = r.stdout.strip() if r.returncode == 0 else ""
    if not head:
        print("no-head: git rev-parse HEAD не сработал")
        return 1
    if done.commit != head:
        print(f"mismatch: done={done.commit} head={head}")
        return 1
    diff_set = _diff_files(worktree, base_sha, head)
    if diff_set is None:
        print("no-diff: ворота не проверены")
        return 1
    if not diff_set:
        print("empty-diff")
        return 1
    for f in done.files:
        if f not in diff_set:
            print(f"unknown-file: {f}")
            return 1
    py = (getattr(project, "python", "") or "").strip() or _sys.executable
    lock = (getattr(project, "test_lock", "") or "").strip() or None
    from hub.gate.acceptance import acceptance_cmd

    # Та же база, что в конвейере: merge-base с рабочей веткой после её слияния.
    work_branch = (getattr(project, "work_branch", "") or "").strip() or None
    gate = check_gate(Path(worktree), base_sha, head, allowed,
                      acceptance_cmd(card_text, py), lock,
                      work_branch=work_branch)
    if not gate.ok:
        for e in gate.errors:
            print(e)
        return 1
    # Панель: ревьюеры из задачи.
    try:
        rev_names = json.loads(task.get("reviewers_json") or "[]")
    except (TypeError, ValueError):
        rev_names = []
    from hub.pipeline.runners import make_runner

    reviewers: dict = {}
    for name in rev_names if isinstance(rev_names, list) else []:
        try:
            reviewers[str(name)] = make_runner(str(name))
        except ValueError:
            continue
    if not reviewers:
        print("no-reviewers: в задаче нет ревьюеров")
        return 1
    round_no = int(task.get("round") or 1) or 1
    _set_stage(store, task_id, f"review r{round_no}", round_no, "ручное ревью")
    _unlink_round_reviews(worktree, round_no)
    diff_text = _diff_text(worktree, base_sha)

    def _one(item) -> None:
        name, runner = item
        prompt = prompts.review_prompt(rules_text, card_text, diff_text,
                                       gate, round_no, blind)
        # Каждому ревьюеру свой файл (как PanelReviewer).
        per_file = f"review_r{round_no}_{name}.json"
        prompt = prompt.replace(f"review_r{round_no}.json", per_file)
        log = str(Path(worktree) / ".agent" / f"reviewer_r{round_no}_{name}.log")
        try:
            rsid = runner.start(prompt, worktree, log)
        except (OSError, RuntimeError, _sp.SubprocessError):
            return
        # Сессия линкуется сразу, как только id известен (до resume).
        try:
            rtool, _m = _runner_tool_model(runner, {"executor": name})
            store.link_session(rsid, rtool, task_id, "reviewer", round_no,
                               str(getattr(runner, "model", name) or name).split("/")[-1])
        except (OSError, sqlite3.Error, ValueError):
            pass
        own = Path(worktree) / ".agent" / per_file
        if not _review_file_valid(own):
            try:
                rsid2 = runner.resume(rsid, REVIEW_FIX_TEXT.replace(
                    "review_rN.json", per_file),
                    worktree, log)
                try:
                    rtool, _m = _runner_tool_model(runner, {"executor": name})
                    store.link_session(rsid2, rtool, task_id, "reviewer", round_no,
                                       str(getattr(runner, "model", name) or name
                                           ).split("/")[-1])
                except (OSError, sqlite3.Error, ValueError):
                    pass
            except (OSError, RuntimeError, _sp.SubprocessError):
                pass

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(reviewers)) as pool:
        list(pool.map(_one, list(reviewers.items())))
    from hub.gate.verdict import verdict as _verdict

    reviews = _collect_reviews(worktree, round_no)
    decision = _verdict(reviews, round_no, max_rounds=max(1, rounds))
    if decision == "ready":
        _set_stage(store, task_id, "ready", round_no, "панель approve")
        print(f"OK {task_id} ready")
        return 0
    if decision == "arbiter":
        _set_stage(store, task_id, "arbiter", round_no, "панель arbiter")
        print(f"ARBITER {task_id}")
        return 1
    print(f"CHANGES {task_id}")
    return 1


def register(subparsers) -> None:
    p = subparsers.add_parser("review", help="только ворота + ревью")
    p.add_argument("task_id", help="ID задачи")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    p.add_argument("--blind", action="store_true", help="без «Решений арбитра»")
    p.set_defaults(func=cmd_review)
