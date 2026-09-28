"""Круги конвейера: preflight → exec → ворота → repair → ревью → вердикт.

Этап пишется в store на каждом переходе + событие `stage`.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from hub import time as ht
from hub.gate.donefile import load_done
from hub.gate.gate import check_gate
from hub.gate.preflight import preflight
from hub.gate.repair import repair_prompt
from hub.gate.verdict import Review, verdict
from hub.pipeline import prompts
from hub.read import opencode as oc

MAX_DIFF_CHARS = 200_000
DIFF_EXCLUDE = (":(exclude)tests/fixtures/**", ":(exclude)**/*.json", ":(exclude)**/eval/**")
_STOP_FILE = "stop_requested"


def _set_stage(store, task_id: str, stage: str, round_no: int, reason: str = "") -> None:
    store.upsert_task(id=task_id, stage=stage, round=round_no, stage_reason=reason[:500])
    try:
        store.add_event(task_id, "stage", {"stage": stage, "round": round_no,
                                           "reason": reason[:500]})
    except (OSError, sqlite3.Error):
        pass


def _resolve_card(task: dict, project) -> Path | None:
    rel = str(task.get("card_path") or "")
    if not rel:
        return None
    p = Path(rel)
    if p.is_absolute() and p.is_file():
        return p
    cands: list[Path] = []
    root = getattr(project, "root", "") or ""
    if root:
        cands.append(Path(root) / rel)
    wt = str(task.get("worktree") or "")
    if wt:
        cands.append(Path(wt) / rel)
    cands.append(Path(rel))
    for c in cands:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def _read_rules(project) -> str:
    raw = str(getattr(project, "rules", "") or "")
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        root = str(getattr(project, "root", "") or "")
        p = Path(root) / raw if root else p
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else ""
    except OSError:
        return ""


def _card_globs(card_text: str) -> list[str]:
    try:
        from hub.gate import lint as lint_mod

        lines = card_text.splitlines()
        sec = lint_mod._section_text(lines, "Можно менять")
        return [str(g) for g in lint_mod._can_change_globs(sec)]
    except (ImportError, AttributeError):
        return []


def _allowed_intersection(card_globs: list[str], project_allowed: list[str]) -> list[str]:
    """«Можно менять» ∩ allowed_paths: glob карточки внутри toml."""
    out: list[str] = []
    for g in card_globs:
        norm = g.removeprefix("./")
        if Path(g).is_absolute() or ".." in Path(g).parts:
            continue
        if any(fnmatch.fnmatch(norm, pat) for pat in (project_allowed or [])):
            out.append(g)
    return out


def _head_sha(worktree: str) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=worktree,
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _diff_files(worktree: str, base: str, head: str) -> set[str] | None:
    try:
        r = subprocess.run(["git", "-c", "core.quotepath=false", "diff", "--no-renames",
                            "--name-only", f"{base}..{head}", "--"],
                           cwd=worktree, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return {l for l in (s.strip() for s in r.stdout.splitlines()) if l}


def _diff_text(worktree: str, base: str) -> str:
    try:
        stat = subprocess.run(["git", "diff", "--stat", f"{base}..HEAD", "--"],
                              cwd=worktree, capture_output=True,
                              text=True, timeout=60)
        r = subprocess.run(["git", "diff", f"{base}..HEAD", "--", ".", *DIFF_EXCLUDE],
                           cwd=worktree, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return ""
    stat_s = stat.stdout if stat.returncode == 0 else ""
    body = r.stdout if r.returncode == 0 else ""
    if len(body) > MAX_DIFF_CHARS:
        body = body[:MAX_DIFF_CHARS] + "\n… дифф обрезан"
    return f"# git diff --stat\n{stat_s}\n# дифф (без фикстур)\n{body}"


def _clean_pycache(worktree: str) -> None:
    """Убрать __pycache__/.pytest_cache: pytest их создаёт, ворота видят грязь."""
    import shutil as _sh

    root = Path(worktree)
    try:
        for p in root.rglob("__pycache__"):
            try:
                # Только внутри worktree, симлинки не трогаем.
                if p.is_dir() and not p.is_symlink():
                    _sh.rmtree(p, ignore_errors=True)
            except OSError:
                continue
        for p in root.rglob("*.pyc"):
            try:
                if p.is_file() and not p.is_symlink():
                    p.unlink()
            except OSError:
                continue
        pc = root / ".pytest_cache"
        try:
            if pc.is_dir() and not pc.is_symlink():
                _sh.rmtree(pc, ignore_errors=True)
        except OSError:
            pass
    except OSError:
        pass


def _runner_tool_model(runner, task: dict) -> tuple[str, str]:
    tool = str(getattr(runner, "tool", "") or "")
    if not tool:
        tool = "agy" if "agy" in type(runner).__name__.lower() else "opencode"
    model = str(getattr(runner, "model", "") or task.get("executor") or "?")
    # Короткое имя модели для roster: полный id режем по `/`.
    short = model.split("/")[-1] if "/" in model else model
    return tool, short


def _task_cost(store, task_id: str, opencode_db: str | None = None) -> tuple[float, float]:
    """Деньги задачи из агрегатов opencode session (Н3): (go, usd)."""
    try:
        links = store.list_sessions(task_id)
    except (OSError, sqlite3.Error):
        return 0.0, 0.0
    if not links:
        return 0.0, 0.0
    db = opencode_db
    if db is None:
        cand = Path.home() / ".local/share/opencode/opencode.db"
        db = str(cand) if cand.exists() else None
    if not db:
        return 0.0, 0.0
    try:
        sessions = oc.sessions(db, 0)
    except (OSError, sqlite3.Error):
        return 0.0, 0.0
    by_id = {s.id: s for s in sessions}
    go = usd = 0.0
    for link in links:
        s = by_id.get(str(link.get("external_id") or ""))
        if s is None:
            continue
        if s.provider == "opencode-go":
            go += float(s.cost or 0.0)
        else:
            usd += float(s.cost or 0.0)
    return go, usd


def _ask_extend(store, task_id: str, text: str) -> None:
    try:
        con = sqlite3.connect(str(store.path))
    except sqlite3.Error:
        return
    try:
        con.execute(
            "INSERT INTO question(task_id, asked_by, text, options_json,"
            " status, answer, answered_via, ts)"
            " VALUES (?, 'claude', ?, ?, 'open', '', '', ?)",
            (task_id, text, json.dumps(["да", "нет"], ensure_ascii=False),
             ht.now_ms()),
        )
        con.commit()
    except sqlite3.Error:
        pass
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def _budget_exceeded(cost: float, budget: float) -> bool:
    return budget > 0 and cost >= budget


def _stop_requested(worktree: str) -> bool:
    try:
        return (Path(worktree) / ".agent" / _STOP_FILE).exists()
    except OSError:
        return False


def _collect_reviews(worktree: str, round_no: int) -> list[Review]:
    """Файлы .agent/review_rN*.json → список Review для verdict()."""
    agent = Path(worktree) / ".agent"
    try:
        if not agent.is_dir():
            return []
        cands = sorted(agent.glob(f"review_r{round_no}*.json"))
    except OSError:
        return []
    import re as _re

    pat = _re.compile(rf"^review_r{round_no}(?:_.*)?\.json$")
    per: list[Path] = [p for p in cands if pat.match(p.name)]
    # Предпочитаем per-reviewer файлы сводному.
    personal = [p for p in per if p.stem != f"review_r{round_no}"]
    use = personal if personal else per
    out: list[Review] = []
    for path in use:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        v = str(data.get("verdict") or "")
        if v not in ("approve", "changes", "dispute"):
            continue
        findings = data.get("findings")
        if v == "approve":
            out.append(Review("approve"))
            continue
        first: dict = {}
        if isinstance(findings, list):
            for f in findings:
                if isinstance(f, dict):
                    first = f
                    break
        fname = str(first.get("file") or first.get("path") or "")
        try:
            lineno = int(first.get("line") or 0)
        except (TypeError, ValueError):
            lineno = 0
        body = str(first.get("issue") or first.get("text")
                    or first.get("message") or first.get("fix") or "")
        out.append(Review(v, file=fname, line=lineno, body=body))
    return out


def _split_runners(runners, task: dict):
    """Нормализовать runners → (executor, {имя: reviewer})."""
    exe = None
    revs: dict = {}
    if isinstance(runners, dict):
        exe = runners.get("executor")
        raw = runners.get("reviewers", runners.get("reviewer", {}))
        if isinstance(raw, dict):
            revs = dict(raw)
        elif isinstance(raw, (list, tuple)):
            revs = {f"r{i}": r for i, r in enumerate(raw)}
    else:
        exe = getattr(runners, "executor", None)
        raw = getattr(runners, "reviewers", getattr(runners, "reviewer", {}))
        if isinstance(raw, dict):
            revs = dict(raw)
        elif isinstance(raw, (list, tuple)):
            revs = {f"r{i}": r for i, r in enumerate(raw)}
    if exe is None:
        # Единственный раннер как исполнитель (тесты с одним).
        exe = runners  # type: ignore[assignment]
    return exe, revs


def run_task(store, project, task_id: str, runners, rounds: int = 2,
             blind: bool = False, opencode_db: str | None = None,
             cost_fn=None) -> str:
    """Вести задачу от карточки до ready/arbiter/failed/stopped.

    Этап пишется в store на каждом переходе + событие `stage`.
    """
    task = store.get_task(task_id)
    if task is None:
        return "failed"
    worktree = str(task.get("worktree") or "")
    base_sha = str(task.get("base_sha") or "")
    if not worktree or not Path(worktree).is_dir():
        _set_stage(store, task_id, "failed", 0, "no-worktree")
        return "failed"
    if not base_sha:
        _set_stage(store, task_id, "failed", 0, "no-base")
        return "failed"
    # Внешний стоп уже стоит — не перезаписываем.
    try:
        fresh = store.get_task(task_id)
        if fresh and str(fresh.get("stage") or "") in ("stopped", "dropped"):
            return str(fresh.get("stage"))
    except (OSError, sqlite3.Error):
        pass

    executor, reviewers = _split_runners(runners, task)
    if not reviewers:
        reviewers = {}

    _set_stage(store, task_id, "preflight", 0, "старт")
    _clean_pycache(worktree)
    try:
        pf = preflight(store, task_id, project)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        _set_stage(store, task_id, "failed", 0, f"preflight-fail: {e}"[:500])
        return "failed"
    if not pf.ok:
        _set_stage(store, task_id, "failed", 0, pf.reason[:500])
        return "failed"
    _clean_pycache(worktree)

    card_path = _resolve_card(store.get_task(task_id) or task, project)
    if card_path is None:
        _set_stage(store, task_id, "failed", 0, "no-card")
        return "failed"
    try:
        card_text = card_path.read_text(encoding="utf-8")
    except OSError as e:
        _set_stage(store, task_id, "failed", 0, f"no-card: {e}"[:500])
        return "failed"
    rules_text = _read_rules(project)
    card_globs = _card_globs(card_text)
    allowed = _allowed_intersection(card_globs, list(getattr(project, "allowed_paths", []) or []))
    py = (getattr(project, "python", "") or "").strip() or sys.executable
    test_cmd = [py, "-m", "pytest", "-q"]
    lock_path = (getattr(project, "test_lock", "") or "").strip() or None
    task_blind = bool(blind or (task.get("blind") if isinstance(task.get("blind"), int) else False))
    try:
        if not task_blind and str(task.get("blind", "")) == "1":
            task_blind = True
    except (AttributeError, TypeError):
        pass
    # Колонка blind из миграции 004 (если есть).
    try:
        if int(task.get("blind") or 0):
            task_blind = True
    except (TypeError, ValueError):
        pass

    budget_go = float(task.get("budget_go") or 0.0)
    try:
        budget_go = float(task.get("budget_go") or 0.0)
    except (TypeError, ValueError):
        budget_go = 0.0
    budget_usd = float(task.get("budget_usd") or 0.0)

    def _cost() -> tuple[float, float]:
        if cost_fn is not None:
            try:
                return cost_fn(store, task_id)
            except (OSError, sqlite3.Error, TypeError, ValueError):
                return 0.0, 0.0
        return _task_cost(store, task_id, opencode_db)

    def _over_budget() -> tuple[bool, float, float]:
        go, usd = _cost()
        if _budget_exceeded(go, budget_go) or _budget_exceeded(usd, budget_usd):
            return True, go, usd
        # 80 % — мягкое событие.
        try:
            if budget_go > 0 and go >= 0.8 * budget_go:
                store.add_event(task_id, "budget_soft", {"go": go, "budget_go": budget_go})
            if budget_usd > 0 and usd >= 0.8 * budget_usd:
                store.add_event(task_id, "budget_soft", {"usd": usd, "budget_usd": budget_usd})
        except (OSError, sqlite3.Error):
            pass
        return False, go, usd

    def _do_budget_stop(reason: str, exec_sid: str | None) -> str:
        over, go, usd = _cost(), 0.0, 0.0
        try:
            go, usd = _cost()
        except (OSError, sqlite3.Error):
            pass
        _set_stage(store, task_id, "stopped", 0, reason[:500])
        try:
            store.add_event(task_id, "budget_hard", {"go": go, "usd": usd})
        except (OSError, sqlite3.Error):
            pass
        _ask_extend(store, task_id,
                     f"Бюджет задачи исчерпан (go ${go:.2f}/{budget_go:.2f}). "
                     f"Продлить на $0.50?")
        if exec_sid:
            try:
                executor.resume(exec_sid, "Бюджет исчерпан: закоммить поимённо "
                                         "и остановись, новых шагов нет.",
                                worktree,
                                str(Path(worktree) / ".agent" / "repair_stop.log"))
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
        return "stopped"

    Path(worktree, ".agent").mkdir(parents=True, exist_ok=True)
    exec_sid: str | None = None
    last_findings: list = []
    last_gate = None

    for round_no in range(1, max(1, int(rounds)) + 1):
        # Внешний стоп / кооперативный флаг.
        try:
            fresh = store.get_task(task_id)
            if fresh and str(fresh.get("stage") or "") in ("stopped", "dropped"):
                return str(fresh.get("stage"))
        except (OSError, sqlite3.Error):
            pass
        if _stop_requested(worktree):
            _set_stage(store, task_id, "stopped", round_no, "stop requested")
            return "stopped"
        over, _go, _usd = _over_budget()
        if over:
            return _do_budget_stop("бюджет 100 %", exec_sid)

        _set_stage(store, task_id, f"exec r{round_no}", round_no, "исполнитель")
        log_exec = str(Path(worktree) / ".agent" / f"executor_r{round_no}.log")
        try:
            if round_no == 1:
                sid = executor.start(prompts.executor_prompt(rules_text, card_text),
                                     worktree, log_exec)
            else:
                from hub.read.findings import dedup_findings, load_findings

                try:
                    items = load_findings(Path(worktree), round_no - 1)
                    last_findings = [{"file": f.file, "line": f.line, "issue": f.issue,
                                      "severity": f.severity, "author": f.author}
                                     for f in dedup_findings(items)]
                except (OSError, ValueError):
                    pass
                sid = executor.resume(
                    exec_sid or "",
                    prompts.fix_prompt(last_findings, last_gate or {}),
                    worktree, log_exec)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            _set_stage(store, task_id, "failed", round_no, f"executor-fail: {e}"[:500])
            return "failed"
        exec_sid = sid
        # Сразу, как только id известен (не в конце).
        try:
            tool, model = _runner_tool_model(executor, store.get_task(task_id) or task)
            store.link_session(sid, tool, task_id, "executor", round_no, model)
        except (OSError, sqlite3.Error):
            pass

        # --- ворота: done.json + дифф + приёмка ---
        _clean_pycache(worktree)
        head = _head_sha(worktree)
        if not head:
            _set_stage(store, task_id, "failed", round_no, "no-head")
            return "failed"
        diff_set = _diff_files(worktree, base_sha, head)
        if diff_set is None:
            _set_stage(store, task_id, "failed", round_no, "no-diff")
            return "failed"
        repair_reason = ""
        try:
            done = load_done(Path(worktree))
        except FileNotFoundError as e:
            repair_reason = str(e)
            done = None
        except ValueError as e:
            repair_reason = str(e)
            done = None
        else:
            if done.commit != head:
                repair_reason = f"mismatch: done={done.commit} head={head}"
            elif any(f not in diff_set for f in done.files):
                bad = next(f for f in done.files if f not in diff_set)
                repair_reason = f"unknown-file: {bad}"
            elif not diff_set and not done.files:
                repair_reason = "empty-diff"
        gate = None
        if not repair_reason:
            try:
                gate = check_gate(Path(worktree), base_sha, head, allowed,
                                  test_cmd, lock_path)
            except (OSError, subprocess.SubprocessError) as e:
                _set_stage(store, task_id, "failed", round_no, f"gate-fail: {e}"[:500])
                return "failed"
            last_gate = gate
            forb = [e for e in gate.errors if e.startswith("forbidden:")]
            if forb:
                # Выход за allowed_paths проекта = failed, за карточку = changes.
                proj_allowed = list(getattr(project, "allowed_paths", []) or [])
                outside = [e for e in forb
                           if not any(fnmatch.fnmatch(e.removeprefix("forbidden: ").strip()
                                                      .removeprefix("./"), pat)
                                      for pat in proj_allowed)]
                if outside:
                    _set_stage(store, task_id, "failed", round_no,
                                "; ".join(outside)[:500])
                    return "failed"
                # Иначе — за карточку: идёт в ревью как changes.
            if any(e == "empty-diff" for e in gate.errors):
                repair_reason = "empty-diff"
            elif any(e.startswith("dirty:") for e in gate.errors):
                repair_reason = "; ".join(e for e in gate.errors
                                          if e.startswith("dirty:"))[:500]
            elif gate.errors and not forb and not any(
                    e.startswith("tests-fail:") for e in gate.errors):
                # Прочие ошибки ворот (git-error, lock) — не чинятся repair.
                if any(e.startswith("locked:") for e in gate.errors):
                    _set_stage(store, task_id, "failed", round_no,
                                "; ".join(gate.errors)[:500])
                    return "failed"
        # --- repair один раз в ту же сессию ---
        if repair_reason:
            _set_stage(store, task_id, f"gate r{round_no}", round_no,
                        f"repair: {repair_reason}"[:500])
            over, _, _ = _over_budget()
            if over:
                return _do_budget_stop("бюджет 100 %", exec_sid)
            try:
                sid2 = executor.resume(exec_sid or "", repair_prompt(repair_reason),
                                       worktree,
                                       str(Path(worktree) / ".agent" / f"repair_r{round_no}.log"))
                try:
                    tool, model = _runner_tool_model(executor, store.get_task(task_id) or task)
                    store.link_session(sid2, tool, task_id, "executor", round_no, model)
                except (OSError, sqlite3.Error):
                    pass
                exec_sid = sid2
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                _set_stage(store, task_id, "failed", round_no, f"repair-fail: {e}"[:500])
                return "failed"
            # Повторная проверка один раз.
            _clean_pycache(worktree)
            head2 = _head_sha(worktree)
            diff2 = _diff_files(worktree, base_sha, head2 or head) if head2 else None
            if not head2 or diff2 is None:
                _set_stage(store, task_id, "failed", round_no, "repair: no-head/no-diff")
                return "failed"
            head, diff_set = head2, diff2
            try:
                done2 = load_done(Path(worktree))
            except (FileNotFoundError, ValueError) as e:
                _set_stage(store, task_id, "failed", round_no, f"repair: {e}"[:500])
                return "failed"
            if done2.commit != head or any(f not in diff_set for f in done2.files):
                _set_stage(store, task_id, "failed", round_no, "repair: done мимо HEAD/диффа")
                return "failed"
            try:
                gate2 = check_gate(Path(worktree), base_sha, head, allowed,
                                   test_cmd, lock_path)
            except (OSError, subprocess.SubprocessError) as e:
                _set_stage(store, task_id, "failed", round_no, f"gate-fail: {e}"[:500])
                return "failed"
            last_gate = gate2
            forb2 = [e for e in gate2.errors if e.startswith("forbidden:")]
            if forb2:
                proj_allowed = list(getattr(project, "allowed_paths", []) or [])
                outside2 = [e for e in forb2
                            if not any(fnmatch.fnmatch(e.removeprefix("forbidden: ").strip()
                                                       .removeprefix("./"), pat)
                                       for pat in proj_allowed)]
                if outside2:
                    _set_stage(store, task_id, "failed", round_no,
                                "; ".join(outside2)[:500])
                    return "failed"
            if any(e == "empty-diff" for e in gate2.errors) or any(
                    e.startswith("dirty:") for e in gate2.errors):
                _set_stage(store, task_id, "failed", round_no,
                            "; ".join(gate2.errors)[:500] or "repair: пусто")
                return "failed"
            gate = gate2
        else:
            _set_stage(store, task_id, f"gate r{round_no}", round_no,
                        "ворота" if not (gate and gate.errors) else "; ".join(gate.errors)[:500])

        # --- панель ревью параллельно ---
        over, _, _ = _over_budget()
        if over:
            return _do_budget_stop("бюджет 100 %", exec_sid)
        if _stop_requested(worktree):
            _set_stage(store, task_id, "stopped", round_no, "stop requested")
            return "stopped"
        _set_stage(store, task_id, f"review r{round_no}", round_no, "ревью")
        diff_text = _diff_text(worktree, base_sha)
        gate_for_prompt = gate if gate is not None else {"commit": True, "scope": [],
                                                         "tests": {"ok": True, "output": ""}}

        def _run_one(item) -> str | None:
            name, runner = item
            over1, _, _ = _over_budget()
            if over1:
                return None
            prompt = prompts.review_prompt(rules_text, card_text, diff_text,
                                           gate_for_prompt, round_no, task_blind)
            log = str(Path(worktree) / ".agent" / f"reviewer_r{round_no}_{name}.log")
            try:
                rsid = runner.start(prompt, worktree, log)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                return None
            try:
                rtool, rmodel = _runner_tool_model(runner, {"executor": name})
                store.link_session(rsid, rtool, task_id, "reviewer", round_no,
                                   str(getattr(runner, "model", name) or name).split("/")[-1])
            except (OSError, sqlite3.Error):
                pass
            return rsid

        if reviewers:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
                list(pool.map(_run_one, list(reviewers.items())))
        over, _, _ = _over_budget()
        if over:
            return _do_budget_stop("бюджет 100 %", exec_sid)

        reviews = _collect_reviews(worktree, round_no)
        try:
            from hub.read.findings import dedup_findings, load_findings

            items = load_findings(Path(worktree), round_no)
            last_findings = [{"file": f.file, "line": f.line, "issue": f.issue,
                              "severity": f.severity, "author": f.author}
                             for f in dedup_findings(items)]
        except (OSError, ValueError):
            last_findings = []
        decision = verdict(reviews, round_no, max_rounds=max(1, int(rounds)))
        if decision == "ready":
            _set_stage(store, task_id, "ready", round_no, "панель approve")
            return "ready"
        if decision == "arbiter":
            reason = ",".join(r.verdict for r in reviews) or "панель молчит"
            _set_stage(store, task_id, "arbiter", round_no, reason[:500])
            return "arbiter"
        # next → следующий круг (если есть).
        if round_no >= max(1, int(rounds)):
            _set_stage(store, task_id, "arbiter", round_no, "круги кончились")
            return "arbiter"
        _set_stage(store, task_id, f"review r{round_no}", round_no, "changes → круг")
        continue
    _set_stage(store, task_id, "arbiter", int(rounds), "круги кончились")
    return "arbiter"
