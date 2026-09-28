"""hub gate: сверка done.json и ворота приёмки. Тонкая обёртка над hub.gate."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from hub.gate.donefile import load_done
from hub.gate.gate import check_gate


def _head_sha(worktree: str) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                           capture_output=True, text=True, timeout=60)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _diff_files(worktree: str, base: str, head: str) -> set[str]:
    try:
        r = subprocess.run(["git", "diff", "--name-only", f"{base}..{head}", "--"],
                           cwd=worktree, capture_output=True, text=True, timeout=60)
    except OSError:
        return set()
    if r.returncode != 0:
        return set()
    return {l for l in (s.strip() for s in r.stdout.splitlines()) if l}


def _parse_can_change_local(card_text: str) -> list[str]:
    """Запасной путь, если hub.gate.lint (H02) ещё нет: glob'ы из «Можно менять»."""
    idx = card_text.find("Можно менять")
    if idx < 0:
        return []
    tail = card_text[idx:]
    m = re.search(r"\n(?:#{1,6}\s|\*\*)", tail[20:])
    section = tail[:20 + m.start()] if m else tail
    return [g for g in (s.strip() for s in re.findall(r"`([^`]+)`", section))
            if g and " " not in g]


def _card_allowed(card: Path | None, project) -> list[str]:
    """Glob'ы «Можно менять»: через hub.gate.lint (H02), иначе локально."""
    if card is not None and card.is_file():
        try:
            import importlib

            lint_mod = importlib.import_module("hub.gate.lint")
        except ImportError:
            lint_mod = None
        if lint_mod is not None:
            for name in ("allowed_globs", "parse_allowed", "allowed_from_card",
                         "card_allowed", "get_allowed", "extract_allowed",
                         "parse_can_change", "can_change_globs"):
                fn = getattr(lint_mod, name, None)
                if not callable(fn):
                    continue
                for argv in ([card] if project is None else ([card], [card, project])):
                    try:
                        got = fn(*argv)
                    except Exception:
                        continue
                    if got:
                        return [str(x) for x in got]
            if project is not None and hasattr(lint_mod, "lint_card"):
                try:
                    res = lint_mod.lint_card(card, project)
                except Exception:
                    res = None
                if res is not None:
                    for attr in ("allowed", "globs", "can_change",
                                 "allowed_paths", "allowed_globs"):
                        got = getattr(res, attr, None)
                        if got:
                            return [str(x) for x in got]
        try:
            return _parse_can_change_local(card.read_text(encoding="utf-8"))
        except OSError:
            return []
    if project is not None and getattr(project, "allowed_paths", None):
        return list(project.allowed_paths)
    return []


def _resolve_card(card_rel: str, project, worktree: str) -> Path | None:
    if not card_rel:
        return None
    p = Path(card_rel)
    if p.is_absolute() and p.is_file():
        return p
    cands: list[Path] = []
    if project is not None and getattr(project, "root", ""):
        cands.append(Path(project.root) / card_rel)
    cands += [Path(worktree) / card_rel, Path(card_rel)]
    for c in cands:
        if c.is_file():
            return c
    return None


def cmd_gate(args) -> int:
    import sys

    from hub.store import Store

    task_id = args.task_id
    task = Store().get_task(task_id)
    if task is None:
        print(f"no-task: {task_id}")
        return 1
    worktree = str(task.get("worktree") or "")
    base_sha = str(task.get("base_sha") or "")
    card_rel = str(task.get("card_path") or "")
    if not worktree or not Path(worktree).is_dir():
        print(f"no-worktree: {worktree or '?'}")
        return 1
    if not base_sha:
        print("no-base: пустой base_sha задачи")
        return 1
    project = None
    lock_path: str | None = None
    try:
        from hub.config import load_project

        hint = Path(getattr(args, "project", None) or worktree)
        project = load_project(hint)
        lock_path = project.test_lock or None
    except Exception:
        project = None
    head_sha = _head_sha(worktree)
    if not head_sha:
        print("no-head: git rev-parse HEAD не сработал")
        return 1

    errors: list[str] = []
    card_path = _resolve_card(card_rel, project, worktree)
    if card_path is None:
        errors.append(f"no-card: {card_rel or '?'}")
        allowed = list(project.allowed_paths) if project else []
    else:
        allowed = _card_allowed(card_path, project)

    diff_files = _diff_files(worktree, base_sha, head_sha)
    done = None
    try:
        done = load_done(Path(worktree))
    except FileNotFoundError as e:
        errors.append(str(e))
    except ValueError as e:
        errors.append(str(e))
    if done is not None:
        if done.commit != head_sha:
            errors.append(f"mismatch: done={done.commit} head={head_sha}")
        for f in done.files:
            if f not in diff_files:
                errors.append(f"unknown-file: {f}")

    if done is not None and done.cmd.strip():
        try:
            test_cmd = shlex.split(done.cmd)
        except ValueError:
            py = project.python if project and project.python else sys.executable
            test_cmd = [py, "-m", "pytest", "-q"]
    else:
        py = project.python if project and project.python else sys.executable
        test_cmd = [py, "-m", "pytest", "-q"]
    res = check_gate(Path(worktree), base_sha, head_sha, allowed, test_cmd, lock_path)
    errors.extend(res.errors)
    if not errors and res.ok:
        print(f"OK {task_id}")
        return 0
    for e in errors:
        print(e)
    return 1


def register(subparsers) -> None:
    p = subparsers.add_parser("gate", help="ворота: done.json, дифф, приёмка")
    p.add_argument("task_id", help="ID задачи в Store")
    p.add_argument("--project", default=None, help="корень проекта (.hub.toml)")
    # Круг информативен: вердикт панели считает H06, ворота проверяют факты git.
    p.add_argument("--round", type=int, default=None)
    p.set_defaults(func=cmd_gate)
