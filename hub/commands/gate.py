"""hub gate: сверка done.json и ворота приёмки. Тонкая обёртка над hub.gate."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from hub.gate.donefile import load_done
from hub.gate.gate import check_gate


def _load_lint():
    """Импорт hub.gate.lint (H02) — единственный путь к разбору карточки."""
    import importlib

    return importlib.import_module("hub.gate.lint")


def _head_sha(worktree: str) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                           capture_output=True, text=True, timeout=60)
    except OSError:
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _diff_files(worktree: str, base: str, head: str) -> tuple[set[str] | None, str]:
    """Файлы base..head (--no-renames: перенос показывает и старый путь);
    при ошибке git — (None, stderr), не пустой set."""
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--no-renames",
                            "--name-only", f"{base}..{head}", "--"],
                           cwd=worktree, capture_output=True, text=True, timeout=60)
    except OSError as e:
        return None, str(e)
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return None, err[-1] if err else f"код {r.returncode}"
    return {l for l in (s.strip() for s in r.stdout.splitlines()) if l}, ""


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


def _cmd_covers(parts: list[str], nodes: list[str]) -> bool:
    """done.cmd гоняет приёмку из карточки: токен pytest + все ноды (голый pytest — всё)."""
    if not any(t == "pytest" or t.endswith("/pytest") or t.endswith("\\pytest")
               for t in parts):
        return False
    if not nodes:
        return True
    blob = " ".join(parts)
    if all(n in blob for n in nodes):
        return True
    explicit = any("tests" in p or p.endswith(".py") or "::" in p for p in parts)
    return not explicit


def cmd_gate(args) -> int:
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
    except FileNotFoundError:
        print("warn: нет .hub.toml — приёмка без замка", file=sys.stderr)
        project = None
    except Exception as e:
        # Битый конфиг — не повод идти без замка: стоим, не продолжаем.
        print(f"config: {type(e).__name__}: {e}")
        return 1
    head_sha = _head_sha(worktree)
    if not head_sha:
        print("no-head: git rev-parse HEAD не сработал")
        return 1

    try:
        lint_mod = _load_lint()
    except ImportError:
        print("no-lint: hub.gate.lint недоступен")
        return 1

    errors: list[str] = []
    card_path = _resolve_card(card_rel, project, worktree)
    if card_path is None:
        errors.append(f"no-card: {card_rel or '?'}")
        allowed: list[str] = []
        nodes: list[str] = []
    else:
        try:
            card_text = card_path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"no-card: {card_path}: {e}")
            return 1
        lines = card_text.splitlines()
        try:
            allowed = [str(g) for g in
                       lint_mod._can_change_globs(lint_mod._section_text(lines, "Можно менять"))]
            nodes = [str(n) for n in
                     lint_mod._pytest_nodes(lint_mod._section_text(lines, "Приёмка"))]
        except AttributeError as e:
            print(f"no-lint: hub.gate.lint без нужной функции: {e}")
            return 1

    diff_files, diff_err = _diff_files(worktree, base_sha, head_sha)
    if diff_files is None:
        errors.append(f"no-diff: {diff_err}" if diff_err else "no-diff")
    done = None
    try:
        done = load_done(Path(worktree))
    except FileNotFoundError as e:
        errors.append(str(e))
    except ValueError as e:
        errors.append(str(e))
    if done is not None:
        if not done.ok:
            errors.append("tests-fail: done.json ok=false")
        if done.commit != head_sha:
            errors.append(f"mismatch: done={done.commit} head={head_sha}")
        if diff_files is not None:
            for f in done.files:
                if f not in diff_files:
                    errors.append(f"unknown-file: {f}")
        if done.cmd:
            try:
                parts = shlex.split(done.cmd)
            except ValueError as e:
                errors.append(f"cmd-mismatch: done.cmd не разбирается: {e}")
            else:
                if not _cmd_covers(parts, nodes):
                    errors.append(f"cmd-mismatch: done={done.cmd!r} мимо Приёмки {nodes}")

    # Приёмка — всегда каноническая, done.cmd — только заявление для сверки.
    # Итог уже не-ok (done.json/дифф/карточка): замок не захватываем, приёмку не гоняем.
    if errors:
        for e in errors:
            print(e)
        return 1
    py = project.python.strip() if project and project.python.strip() else sys.executable
    res = check_gate(Path(worktree), base_sha, head_sha, allowed,
                     [py, "-m", "pytest", "-q"], lock_path)
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
