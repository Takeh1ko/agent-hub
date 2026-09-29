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
import time
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
    """Глобы карточки через общий разбор (brace-группы раскрыты)."""
    try:
        from hub.pipeline.common import card_globs as _cg

        return _cg(card_text)
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
    from hub.pipeline.common import clean_pycache

    clean_pycache(worktree)


REVIEW_FIX_TEXT = (
    "Файл `.agent/review_rN.json` отсутствует или не JSON по схеме "
    '{"verdict": "approve" | "changes" | "dispute", '
    '"findings": [{"severity": "high|medium|low", "file": ..., '
    '"line": ..., "issue": ..., "fix": ...}]}. '
    "Запиши его сейчас своим инструментом записи (только JSON, без пояснений вокруг)."
)


def _review_file_valid(path: Path) -> bool:
    """Per-reviewer файл — валидный вердикт для панели."""
    try:
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if str(data.get("verdict") or "") not in ("approve", "changes", "dispute"):
        return False
    findings = data.get("findings", [])
    return isinstance(findings, list) and all(isinstance(f, dict) for f in findings)


def _unlink_round_reviews(worktree: str, round_no: int) -> None:
    """Убрать stale-вердикты круга перед новой панелью.

    Иначе упавший ревьюер засчитает свой старый approve (ложный ready).
    """
    agent = Path(worktree) / ".agent"
    try:
        for p in sorted(agent.glob(f"review_r{round_no}*.json")):
            import re as _re

            if _re.match(rf"^review_r{round_no}(?:_.*)?\.json$", p.name):
                try:
                    if p.is_file() and not p.is_symlink():
                        p.unlink()
                except OSError:
                    continue
    except OSError:
        pass


def _continued_flag(store, task_id: str) -> bool:
    from hub.pipeline.common import meta_get

    return (meta_get(store, f"continued:{task_id}") or "") == "1"


def _consume_continued_flag(store, task_id: str) -> None:
    from hub.pipeline.common import meta_del

    meta_del(store, f"continued:{task_id}")


def _preflight_continued(store, project, task_id: str,
                         worktree: str, base_sha: str):
    """Предполёт для продолженной задачи (continue): HEAD уже впереди базы.

    Штатный preflight требует HEAD == base_sha, что противоречит смыслу
    continue (та же ветка, база = merge-base). Проверяем то же самое,
    кроме равенства HEAD: точка ответвления (merge-base work_branch/ветки)
    должна совпадать с base_sha. Файлы H02 не трогаем.
    """
    import hashlib as _hl

    from hub.gate.preflight import PreflightResult, run_hook
    from hub.read.git import is_dirty
    from hub.read.procs import lock_holder

    task = store.get_task(task_id)
    if task is None:
        return PreflightResult(ok=False, reason="no-task")
    wt = Path(worktree)
    if not wt.is_dir():
        return PreflightResult(ok=False, reason="dirty")
    try:
        if is_dirty(worktree):
            return PreflightResult(ok=False, reason="dirty")
    except (OSError, subprocess.SubprocessError):
        return PreflightResult(ok=False, reason="dirty")
    # База продолжения — merge-base, а не HEAD.
    branch = str((task.get("branch") or f"agent/{task_id}"))
    root = str(getattr(project, "root", "") or "")
    work_branch = (getattr(project, "work_branch", "") or "").strip() or "HEAD"
    if root:
        from hub.pipeline.common import merge_base as _mb

        mb = _mb(root, work_branch, branch)
        if not mb:
            return PreflightResult(ok=False, reason="no-merge-base")
        h = (base_sha or "").strip()
        if h != mb and not (len(h) >= 7 and len(mb) >= 7
                            and (h.startswith(mb) or mb.startswith(h))):
            return PreflightResult(ok=False, reason="base-moved")
    rp_raw = str(getattr(project, "rules", "") or "")
    rp = Path(rp_raw) if Path(rp_raw).is_absolute() else (
        Path(root) / rp_raw if root and rp_raw else Path(rp_raw))
    if not str(rp) or not rp.is_file():
        return PreflightResult(ok=False, reason="no-rules")
    hook = (project.hooks.task_setup or "").strip() if getattr(project, "hooks", None) else ""
    if hook:
        env = {"HUB_TASK_ID": task_id, "HUB_WORKTREE": worktree,
               "HUB_PROJECT_ROOT": root or ""}
        try:
            code, out_tail, err_tail = run_hook(hook, env, Path(worktree))
        except (OSError, subprocess.SubprocessError) as e:
            return PreflightResult(ok=False, reason=f"setup-fail: {e}"[:2000])
        if code != 0:
            raw = (err_tail.strip() or out_tail.strip())
            one = " ".join(raw.split())
            return PreflightResult(ok=False, reason=f"setup-fail: {one[-2000:] or f'код {code}'}")
    python = (getattr(project, "python", "") or "").strip()
    if python:
        if not Path(python).exists():
            return PreflightResult(ok=False, reason="collect-fail")
        py = python
    else:
        py = sys.executable
    try:
        r = subprocess.run([py, "-m", "pytest", "--collect-only", "-q"],
                           cwd=worktree, capture_output=True,
                           text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return PreflightResult(ok=False, reason="collect-fail")
    if r.returncode != 0:
        return PreflightResult(ok=False, reason="collect-fail")
    lock_path = (getattr(project, "test_lock", "") or "").strip()
    if lock_path:
        try:
            holder = lock_holder(lock_path)
        except (OSError, subprocess.SubprocessError) as e:
            return PreflightResult(ok=False, reason=f"lock-fail: {e}"[:2000])
        if holder is not None:
            # Формат как в H02 (pid + время), иначе причины расходятся.
            try:
                when = ht.fmt_local(holder.started_ms)
            except (OSError, ValueError, AttributeError, TypeError):
                when = str(holder.started_ms)
            # Как основной preflight: занятый общий замок — не отказ, ворота дождутся.
            pass
    try:
        digest = _hl.sha256(rp.read_bytes()).hexdigest()
        store.upsert_task(id=task_id, rules_sha=digest)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        return PreflightResult(ok=False, reason=f"store-fail: {e}"[:2000])
    return PreflightResult(ok=True, reason="")


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
    """Лимит Go: 0 — лимит не задан."""
    return budget > 0 and cost >= budget


def _usd_exceeded(cost: float, budget: float) -> bool:
    """Лимит usd: 0 — запрет трат реальных денег (стоп при usd > 0)."""
    if budget > 0:
        return cost >= budget
    return cost > 0


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
        dicts: list = []
        if isinstance(findings, list):
            dicts = [f for f in findings if isinstance(f, dict)]
            # Судьба dispute не должна зависеть от порядка: сначала ищем
            # finding с file:line, иначе берём первый.
            def _has_pos(f: dict) -> bool:
                if not str(f.get("file") or f.get("path") or ""):
                    return False
                try:
                    return int(f.get("line") or 0) > 0
                except (TypeError, ValueError):
                    return False

            first = next((f for f in dicts if _has_pos(f)),
                         dicts[0] if dicts else {})
        fname = str(first.get("file") or first.get("path") or "")
        try:
            lineno = int(first.get("line") or 0)
        except (TypeError, ValueError):
            lineno = 0
        # Обоснование — самый длинный текст среди всех findings: file:line
        # и суть могут лежать в разных элементах, судьба dispute не должна
        # зависеть от распределения полей между ними.
        def _text(f: dict) -> str:
            return str(f.get("issue") or f.get("text")
                       or f.get("message") or f.get("fix") or "")

        body = max((_text(f) for f in dicts), key=len, default="")
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


def _silence_secs(exc: BaseException) -> int | None:
    """Секунды тишины из `RuntimeError("opencode: тишина N c")`, иначе None.

    Совпадение только по полному шаблону `тишина N c`: чужой текст
    со словом «тишина» без счётчика — не сработка сторожа.
    """
    try:
        msg = str(exc)
    except Exception:
        return None
    if "тишина" not in msg:
        return None
    import re as _re

    m = _re.search(r"тишина\s+(\d+)\s*c", msg)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (TypeError, ValueError):
        return None


def _silence_sid(exc: BaseException) -> str | None:
    """SessionID из ошибки тишины (`... тишина N c sid=<id> ...`), иначе None.

    Сторож дописывает sid, увиденный в stdout до тишины, — fallback
    продолжает ту же сессию на muse через resume, а не start.
    """
    try:
        msg = str(exc)
    except Exception:
        return None
    import re as _re

    m = _re.search(r"sid=([\w.\-]+)", msg)
    if not m:
        return None
    return m.group(1) or None


def _is_musefree_task(store, task_id: str, executor, task: dict) -> bool:
    """Исполнитель задачи — бесплатный Spark (только musefree).

    Поле задачи `executor == "musefree"` или модель — musefree-id
    (`opencode/muse-spark-*-free`). Другие free-модели (mimofree и т.п.)
    сюда не попадают: карточка ограничивает fallback исполнителем musefree.
    """
    try:
        cur = store.get_task(task_id) or task
    except (OSError, sqlite3.Error):
        cur = task
    try:
        if str((cur or {}).get("executor") or "") == "musefree":
            return True
    except (AttributeError, TypeError):
        pass
    try:
        model = str(getattr(executor, "model", "") or "")
    except (AttributeError, ValueError):
        model = ""
    if not model:
        return False
    try:
        from hub.pipeline.runners import MODELS as _MODELS
    except ImportError:
        _MODELS = {}
    musefree_id = ""
    try:
        musefree_id = str((_MODELS.get("musefree") or ("", None))[0] or "")
    except (AttributeError, TypeError, IndexError):
        musefree_id = ""
    if musefree_id and model == musefree_id:
        return True
    return "muse-spark" in model and "free" in model


def _apply_idle_from_project(project, runners_list: list) -> None:
    """Порог тишины из конфига проекта — в раннеры (без смены queue.py)."""
    try:
        idle = int(getattr(project, "idle_s", 900))
    except (TypeError, ValueError, AttributeError):
        return
    for r in runners_list:
        try:
            if hasattr(r, "idle_s"):
                r.idle_s = idle
        except (AttributeError, ValueError):
            continue


def _make_muse_runner(executor):
    """Раннер Spark Go для fallback после тишины musefree (та же сессия)."""
    from hub.pipeline import runners as _rm

    try:
        timeout = int(getattr(executor, "timeout_s", 90 * 60))
    except (TypeError, ValueError):
        timeout = 90 * 60
    try:
        idle = int(getattr(executor, "idle_s", 900))
    except (TypeError, ValueError):
        idle = 900
    try:
        return _rm.make_runner("muse", timeout_s=timeout, idle_s=idle)
    except TypeError:
        return _rm.make_runner("muse")


def _log_silence_fallback(store, task_id: str, secs: int) -> None:
    """Событие в журнал задачи: бесплатный Spark молчал → Spark Go."""
    try:
        if secs >= 60:
            mins = secs // 60
            txt = f"бесплатный Spark молчал {mins} мин ({secs} c) → Spark Go"
        else:
            txt = f"бесплатный Spark молчал {secs} c → Spark Go"
        store.add_event(task_id, "stuck", {"reason": txt, "from": "musefree",
                                           "to": "muse", "silence_s": secs})
    except (OSError, sqlite3.Error, ValueError):
        pass
    try:
        store.upsert_task(id=task_id, executor="muse")
    except (OSError, sqlite3.Error, ValueError):
        pass


def _retry_cfg(project) -> tuple[int, float]:
    """Повторы при сбое сети/сервера: (retry_max, retry_pause_s)."""
    try:
        max_r = int(getattr(project, "retry_max", 3))
    except (TypeError, ValueError, AttributeError):
        max_r = 3
    if max_r < 0:
        max_r = 0
    if max_r > 10:
        max_r = 10
    try:
        base = float(getattr(project, "retry_pause_s", 120))
    except (TypeError, ValueError, AttributeError):
        base = 120.0
    if base < 0:
        base = 0.0
    return max_r, base


def _is_transient_exc(exc: BaseException) -> bool:
    """Ошибка — транзиентный сбой сети/сервера (TransientError)."""
    try:
        from hub.pipeline.runners import TransientError as _TE

        if isinstance(exc, _TE):
            return True
    except ImportError:
        pass
    try:
        return type(exc).__name__ == "TransientError"
    except Exception:
        return False


def _fmt_pause(pause: float) -> str:
    try:
        f = float(pause)
    except (TypeError, ValueError):
        return str(pause)
    if f.is_integer():
        return str(int(f))
    # Доли секунды в тестах — коротко, без хвостов float.
    s = f"{f:.3f}".rstrip("0").rstrip(".")
    return s or "0"


def _log_retry(store, task_id: str, err_text: str,
               n: int, max_n: int, pause_s: float) -> None:
    """Событие в журнал на каждый повтор: «сбой opencode (…) → повтор N/M …»."""
    try:
        short = " ".join(str(err_text or "").split())[:80]
    except (ValueError, AttributeError):
        short = ""
    txt = (f"сбой opencode ({short}) → повтор {n}/{max_n} "
           f"через {_fmt_pause(pause_s)} с")
    try:
        store.add_event(task_id, "stuck", {"reason": txt})
    except (OSError, sqlite3.Error, ValueError):
        pass


def _transient_reason(err_text: str) -> str:
    """Причина arbiter при исчерпанных повторах (не «панель молчит»)."""
    try:
        t = " ".join(str(err_text or "").split())
    except (ValueError, AttributeError):
        t = ""
    return f"сбой сети/сервера opencode: {t}"[:500]


def _retry_sleep(secs: float) -> None:
    """Пауза перед повтором (своя функция модуля для тестов).

    Тесты подменяют только её (`monkeypatch.setattr(cyc, "_retry_sleep", …)`),
    а не глобальный `time.sleep`: иначе в окно теста попадают сны
    subprocess/git и тест `test_retry_pause_grows_x2` красный в трети прогонов.
    """
    try:
        time.sleep(secs)
    except (OSError, ValueError, OverflowError):
        pass


def _transient_sid(exc: BaseException) -> str | None:
    """SessionID из TransientError (увиден в stdout до сбоя), иначе None.

    Исполнитель повторяет ту же сессию (`--session`), ревьюер — новой.
    """
    try:
        sid = getattr(exc, "session_id", None)
    except Exception:
        return None
    try:
        s = str(sid or "").strip()
    except (AttributeError, ValueError, TypeError):
        return None
    return s or None


def _with_transient_retry(store, task_id: str, fn, retry_max: int,
                          retry_base: float):
    """Вызвать fn() с повторами при TransientError (до retry_max раз).

    Возвращает результат fn. Исчерпали повторы — пробрасывает TransientError
    с последним текстом (вызывающая сторона идёт в arbiter).
    Не транзиентные ошибки пробрасываются сразу.
    """
    last_text = ""
    last_sid: str | None = None
    total = max(0, int(retry_max)) + 1
    for attempt in range(total):
        try:
            return fn()
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            if not _is_transient_exc(e):
                raise
            try:
                last_text = str(e) or "сбой opencode"
            except Exception:
                last_text = "сбой opencode"
            try:
                _sid = _transient_sid(e)
                if _sid:
                    last_sid = _sid
            except (AttributeError, ValueError):
                pass
            if attempt >= max(0, int(retry_max)):
                try:
                    from hub.pipeline.runners import TransientError as _TE

                    raise _TE(last_text[:2000],
                              session_id=last_sid) from e
                except (ImportError, TypeError):
                    raise
            n = attempt + 1
            try:
                pause = float(retry_base) * (2 ** (n - 1)) if retry_base else 0.0
            except (TypeError, ValueError):
                pause = 0.0
            _log_retry(store, task_id, last_text, n, max(0, int(retry_max)), pause)
            try:
                if pause and pause > 0:
                    _retry_sleep(pause)
            except (OSError, ValueError, OverflowError):
                pass
            continue
    try:
        from hub.pipeline.runners import TransientError as _TE

        raise _TE((last_text or "сбой opencode")[:2000],
                  session_id=last_sid)
    except (ImportError, TypeError):
        raise RuntimeError(last_text or "сбой opencode")


def _exec_with_retry(store, task_id: str, executor, exec_prompt: str,
                     worktree: str, log_exec: str, retry_max: int,
                     retry_base: float, round_no: int,
                     use_old_session: bool, old_exec_sid: str | None,
                     exec_sid: str | None):
    """Шаг исполнителя с повторами: та же сессия, если sid уже получен.

    Круг 1 свежий старт: первая попытка — `start`, при `TransientError`
    с `session_id` (sid уже был в stdout до сбоя) повтор — `resume(sid)`,
    без sid — снова `start` (новая сессия). Круг ≥2 / continue старой
    сессией / repair — всегда `resume` того же sid. Событие и пауза ×2 —
    как `_with_transient_retry`; исчерпали — проброс `TransientError`.
    """
    last_sid: str | None = None
    last_text = ""
    total = max(0, int(retry_max)) + 1
    for attempt in range(total):
        # Какую сессию продолжаем на этой попытке.
        resume_sid: str | None = None
        do_start = False
        if round_no == 1 and use_old_session and old_exec_sid and last_sid is None:
            resume_sid = old_exec_sid
        elif last_sid:
            resume_sid = last_sid
        elif round_no == 1 and attempt == 0 and not (use_old_session and old_exec_sid):
            do_start = True
        elif round_no == 1 and last_sid is None:
            # Повтор без sid — новая сессия.
            do_start = True
        else:
            resume_sid = exec_sid or old_exec_sid or last_sid
            if not resume_sid:
                do_start = True
        try:
            if do_start:
                return executor.start(exec_prompt, worktree, log_exec)
            return executor.resume(resume_sid or "", exec_prompt,
                                   worktree, log_exec)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            if not _is_transient_exc(e):
                raise
            try:
                last_text = str(e) or "сбой opencode"
            except Exception:
                last_text = "сбой opencode"
            try:
                _sid = _transient_sid(e)
                if _sid:
                    last_sid = _sid
            except (AttributeError, ValueError):
                pass
            if attempt >= max(0, int(retry_max)):
                try:
                    from hub.pipeline.runners import TransientError as _TE

                    raise _TE(last_text[:2000],
                              session_id=last_sid) from e
                except (ImportError, TypeError):
                    raise
            n = attempt + 1
            try:
                pause = float(retry_base) * (2 ** (n - 1)) if retry_base else 0.0
            except (TypeError, ValueError):
                pause = 0.0
            _log_retry(store, task_id, last_text, n, max(0, int(retry_max)), pause)
            try:
                if pause and pause > 0:
                    _retry_sleep(pause)
            except (OSError, ValueError, OverflowError):
                pass
            continue
    try:
        from hub.pipeline.runners import TransientError as _TE

        raise _TE((last_text or "сбой opencode")[:2000],
                  session_id=last_sid)
    except (ImportError, TypeError):
        raise RuntimeError(last_text or "сбой opencode")


def _review_fix_with_retry(store, task_id: str, runner, prompt: str,
                           worktree: str, log: str, per_file: str,
                           rsid: str, retry_max: int, retry_base: float):
    """Добивка ревьюера (`_do_fix`): первая попытка — та же сессия, повторы — новой.

    Ревьюер по контракту H13 — всегда новой сессией: если `resume(rsid)`
    упал транзиентно, повтор — `start` (новый sid пишет тот же per-файл).
    Всего 1 + retry_max вызовов (как у остальных шагов), на каждый повтор —
    событие и пауза ×2. Успех возвращает sid (новый или старый).
    """
    last_text = ""
    total = max(0, int(retry_max)) + 1
    for attempt in range(total):
        try:
            if attempt == 0:
                return runner.resume(rsid, prompt, worktree, log)
            return runner.start(prompt, worktree, log)
        except (OSError, RuntimeError, subprocess.SubprocessError) as e2:
            if not _is_transient_exc(e2):
                raise
            try:
                last_text = str(e2) or "сбой opencode"
            except Exception:
                last_text = "сбой opencode"
            if attempt >= max(0, int(retry_max)):
                try:
                    from hub.pipeline.runners import TransientError as _TE

                    raise _TE(last_text[:2000]) from e2
                except ImportError:
                    raise
            n = attempt + 1
            try:
                pause = float(retry_base) * (2 ** (n - 1)) if retry_base else 0.0
            except (TypeError, ValueError):
                pause = 0.0
            _log_retry(store, task_id, last_text, n, max(0, int(retry_max)), pause)
            try:
                if pause and pause > 0:
                    _retry_sleep(pause)
            except (OSError, ValueError, OverflowError):
                pass
            continue
    try:
        from hub.pipeline.runners import TransientError as _TE

        raise _TE((last_text or "сбой opencode")[:2000])
    except ImportError:
        raise RuntimeError(last_text or "сбой opencode")


def _last_exec_sid(store, task_id: str) -> str | None:
    """Последняя сессия исполнителя задачи (для продолжения без смены карточки)."""
    try:
        sessions = store.list_sessions(task_id)
    except (OSError, sqlite3.Error, ValueError):
        return None
    last: str | None = None
    try:
        for s in sessions:
            try:
                if str(s.get("role") or "") != "executor":
                    continue
            except (AttributeError, TypeError):
                continue
            eid = str(s.get("external_id") or "").strip()
            if eid:
                last = eid
    except (TypeError, ValueError):
        return last
    return last


def _exec_fresh_required(store, task_id: str) -> bool:
    """Continue просил новую сессию (карточка изменена)."""
    try:
        from hub.pipeline.common import meta_get as _mg

        return (_mg(store, f"exec_new_session:{task_id}") or "") == "1"
    except (OSError, ValueError, sqlite3.Error):
        return False


def _consume_exec_fresh(store, task_id: str) -> None:
    try:
        from hub.pipeline.common import meta_del as _md

        _md(store, f"exec_new_session:{task_id}")
    except (OSError, ValueError, sqlite3.Error):
        pass


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
    # Порог тишины из конфига — в раннеры (queue.py не меняем).
    try:
        _apply_idle_from_project(project, [executor, *list(reviewers.values())])
    except (OSError, ValueError, AttributeError):
        pass
    _silence_fallback_done = False
    try:
        retry_max, retry_base = _retry_cfg(project)
    except (OSError, ValueError, AttributeError):
        retry_max, retry_base = 3, 120.0

    # H13 п.4: взятая очередью задача уже «exec rN» (_mark_taken в _run_one) —
    # предполёт её не затирает, иначе метка видна лишь миллисекунды.
    try:
        _cur = store.get_task(task_id) or {}
        _cur_stage = str(_cur.get("stage") or "")
    except (OSError, sqlite3.Error, ValueError, AttributeError):
        _cur_stage = ""
    _is_work = (_cur_stage == "preflight" or _cur_stage.startswith("exec r")
                or _cur_stage.startswith("gate r") or _cur_stage.startswith("review r"))
    if not _is_work:
        _set_stage(store, task_id, "preflight", 0, "старт")
    _clean_pycache(worktree)
    continued = _continued_flag(store, task_id)
    # H13 п.3: новая или прежняя сессия исполнителя после continue.
    # Единственное место решения — флаг exec_new_session от continue
    # (card_hash и событие пишет только continue_.py).
    exec_fresh = False
    old_exec_sid: str | None = None
    if continued:
        try:
            exec_fresh = _exec_fresh_required(store, task_id)
        except (OSError, ValueError, sqlite3.Error):
            exec_fresh = False
        if not exec_fresh:
            try:
                old_exec_sid = _last_exec_sid(store, task_id)
            except (OSError, ValueError, sqlite3.Error):
                old_exec_sid = None
    try:
        if continued:
            pf = _preflight_continued(store, project, task_id, worktree, base_sha)
        else:
            pf = preflight(store, task_id, project)
    except (OSError, sqlite3.Error, subprocess.SubprocessError) as e:
        _set_stage(store, task_id, "failed", 0, f"preflight-fail: {e}"[:500])
        return "failed"
    if not pf.ok:
        # Флаг продолжения съедаем только после успеха: транзиентный провал
        # (dirty/locked/collect-fail) иначе переведёт повтор на строгий
        # HEAD == base_sha и даст base-moved без причины.
        _set_stage(store, task_id, "failed", 0, pf.reason[:500])
        return "failed"
    if continued:
        _consume_continued_flag(store, task_id)
        if exec_fresh:
            _consume_exec_fresh(store, task_id)
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
    from hub.gate.acceptance import acceptance_cmd

    test_cmd = acceptance_cmd(card_text, py)  # приёмка карточки, не весь набор
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

    budget_go = 0.0
    try:
        budget_go = float(task.get("budget_go") or 0.0)
    except (TypeError, ValueError):
        budget_go = 0.0
    budget_usd = 0.0
    try:
        budget_usd = float(task.get("budget_usd") or 0.0)
    except (TypeError, ValueError):
        budget_usd = 0.0

    def _cost() -> tuple[float, float]:
        if cost_fn is not None:
            try:
                return cost_fn(store, task_id)
            except (OSError, sqlite3.Error, TypeError, ValueError):
                return 0.0, 0.0
        return _task_cost(store, task_id, opencode_db)

    _soft_sent: set[str] = set()

    def _over_budget(round_no: int = 0) -> tuple[bool, float, float]:
        go, usd = _cost()
        if _budget_exceeded(go, budget_go) or _usd_exceeded(usd, budget_usd):
            return True, go, usd
        # 80 % — мягкое событие, один раз на круг (не спамим).
        key = f"soft:{round_no}"
        if key not in _soft_sent and (
                (budget_go > 0 and go >= 0.8 * budget_go)
                or (budget_usd > 0 and usd >= 0.8 * budget_usd)):
            _soft_sent.add(key)
            try:
                store.add_event(task_id, "budget_soft",
                                {"go": go, "budget_go": budget_go,
                                 "usd": usd, "budget_usd": budget_usd})
            except (OSError, sqlite3.Error):
                pass
        return False, go, usd

    def _do_budget_stop(reason: str, exec_sid: str | None, round_no: int = 0,
                        extend_usd: float = 0.5) -> str:
        try:
            go, usd = _cost()
        except (OSError, sqlite3.Error):
            go, usd = 0.0, 0.0
        # Какой счётчик сработал — в причину и в вопрос, оба — в текст:
        # иначе usd-стоп спрашивает «продлить go $0.00/0.00».
        hit = "/".join([s for s, bad in (("go", _budget_exceeded(go, budget_go)),
                                          ("usd", _usd_exceeded(usd, budget_usd)))
                        if bad]) or "?"
        full_reason = f"{reason} ({hit})"[:500]
        _set_stage(store, task_id, "stopped", round_no, full_reason)
        try:
            store.add_event(task_id, "budget_hard", {"go": go, "usd": usd})
        except (OSError, sqlite3.Error):
            pass
        _ask_extend(store, task_id,
                     f"Бюджет задачи исчерпан ({hit}: "
                     f"go ${go:.2f}/{budget_go:.2f}, "
                     f"usd ${usd:.2f}/{budget_usd:.2f}). "
                     f"Продлить на ${extend_usd:.2f}?")
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
        over, _go, _usd = _over_budget(round_no)
        if over:
            return _do_budget_stop("бюджет 100 %", exec_sid, round_no)

        _set_stage(store, task_id, f"exec r{round_no}", round_no, "исполнитель")
        log_exec = str(Path(worktree) / ".agent" / f"executor_r{round_no}.log")
        # Промпт один на все повторы шага.
        if round_no == 1:
            exec_prompt = prompts.executor_prompt(rules_text, card_text)
        else:
            from hub.read.findings import dedup_findings, load_findings

            try:
                items = load_findings(Path(worktree), round_no - 1)
                last_findings = [{"file": f.file, "line": f.line, "issue": f.issue,
                                  "severity": f.severity, "author": f.author}
                                 for f in dedup_findings(items)]
            except (OSError, ValueError):
                pass
            exec_prompt = prompts.fix_prompt(last_findings, last_gate or {})
        # H13 п.3: круг 1 после continue без смены карточки — та же сессия.
        use_old_session = bool(round_no == 1 and continued
                               and not exec_fresh and old_exec_sid)

        # H13 п.2: при TransientError с известным sid — повтор той же
        # сессией (`--session`), без sid в круге 1 — новой (start).
        try:
            try:
                sid = _exec_with_retry(store, task_id, executor, exec_prompt,
                                       worktree, log_exec, retry_max,
                                       retry_base, round_no,
                                       use_old_session, old_exec_sid,
                                       exec_sid)
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                if _is_transient_exc(e):
                    _set_stage(store, task_id, "arbiter", round_no,
                               _transient_reason(str(e)))
                    return "arbiter"
                raise
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            secs = _silence_secs(e)
            if (secs is not None and not _silence_fallback_done
                    and _is_musefree_task(store, task_id, executor, task)):
                _silence_fallback_done = True
                try:
                    executor = _make_muse_runner(executor)
                except (ValueError, OSError, RuntimeError):
                    _set_stage(store, task_id, "failed", round_no,
                               f"executor-fail: {e}"[:500])
                    return "failed"
                _log_silence_fallback(store, task_id, secs)
                # Та же сессия продолжается на muse: sid из stdout до тишины
                # (или exec_sid прошлых кругов) — через resume; sid нет
                # (тишина с первой секунды) — новый start в круге 1.
                resume_sid = exec_sid or _silence_sid(e)
                # H13: fallback-вызов тоже с повторами при сбое сети/сервера.

                def _do_fallback():
                    if resume_sid:
                        if round_no == 1:
                            return executor.resume(
                                resume_sid,
                                prompts.executor_prompt(rules_text, card_text),
                                worktree, log_exec)
                        return executor.resume(
                            resume_sid,
                            prompts.fix_prompt(last_findings, last_gate or {}),
                            worktree, log_exec)
                    if round_no == 1:
                        return executor.start(
                            prompts.executor_prompt(rules_text, card_text),
                            worktree, log_exec)
                    return executor.start(
                        prompts.executor_prompt(rules_text, card_text),
                        worktree, log_exec)

                try:
                    try:
                        sid = _with_transient_retry(store, task_id, _do_fallback,
                                                    retry_max, retry_base)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as e2:
                        if _is_transient_exc(e2):
                            _set_stage(store, task_id, "arbiter", round_no,
                                       _transient_reason(str(e2)))
                            return "arbiter"
                        raise
                except (OSError, RuntimeError, subprocess.SubprocessError) as e2:
                    _set_stage(store, task_id, "failed", round_no,
                               f"executor-fail: {e2}"[:500])
                    return "failed"
            else:
                _set_stage(store, task_id, "failed", round_no, f"executor-fail: {e}"[:500])
                return "failed"
        exec_sid = sid
        # Сразу, как только id известен (не в конце).
        try:
            tool, model = _runner_tool_model(executor, store.get_task(task_id) or task)
            store.link_session(sid, tool, task_id, "executor", round_no, model)
        except (OSError, sqlite3.Error, ValueError):
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
            over, _, _ = _over_budget(round_no)
            if over:
                return _do_budget_stop("бюджет 100 %", exec_sid, round_no)
            repair_log = str(Path(worktree) / ".agent" / f"repair_r{round_no}.log")
            repair_text = repair_prompt(repair_reason)

            def _do_repair():
                return executor.resume(exec_sid or "", repair_text,
                                       worktree, repair_log)

            try:
                try:
                    sid2 = _with_transient_retry(store, task_id, _do_repair,
                                                 retry_max, retry_base)
                except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                    if _is_transient_exc(e):
                        _set_stage(store, task_id, "arbiter", round_no,
                                   _transient_reason(str(e)))
                        return "arbiter"
                    raise
                try:
                    tool, model = _runner_tool_model(executor, store.get_task(task_id) or task)
                    store.link_session(sid2, tool, task_id, "executor", round_no, model)
                except (OSError, sqlite3.Error):
                    pass
                exec_sid = sid2
            except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                secs = _silence_secs(e)
                if (secs is not None and not _silence_fallback_done
                        and _is_musefree_task(store, task_id, executor, task)):
                    _silence_fallback_done = True
                    try:
                        executor = _make_muse_runner(executor)
                    except (ValueError, OSError, RuntimeError):
                        _set_stage(store, task_id, "failed", round_no,
                                   f"repair-fail: {e}"[:500])
                        return "failed"
                    _log_silence_fallback(store, task_id, secs)

                    def _do_repair_fallback():
                        return executor.resume(
                            exec_sid or _silence_sid(e) or "", repair_text,
                            worktree, repair_log)

                    try:
                        try:
                            sid2 = _with_transient_retry(
                                store, task_id, _do_repair_fallback,
                                retry_max, retry_base)
                        except (OSError, RuntimeError, subprocess.SubprocessError) as e2:
                            if _is_transient_exc(e2):
                                _set_stage(store, task_id, "arbiter", round_no,
                                           _transient_reason(str(e2)))
                                return "arbiter"
                            raise
                        try:
                            tool, model = _runner_tool_model(
                                executor, store.get_task(task_id) or task)
                            store.link_session(sid2, tool, task_id,
                                               "executor", round_no, model)
                        except (OSError, sqlite3.Error):
                            pass
                        exec_sid = sid2
                    except (OSError, RuntimeError, subprocess.SubprocessError) as e2:
                        _set_stage(store, task_id, "failed", round_no,
                                   f"repair-fail: {e2}"[:500])
                        return "failed"
                else:
                    _set_stage(store, task_id, "failed", round_no,
                               f"repair-fail: {e}"[:500])
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
        # Stale-вердикты круга убираем заранее (иначе упавший ревьюер
        # засчитает старый approve).
        _unlink_round_reviews(worktree, round_no)
        over, _, _ = _over_budget(round_no)
        if over:
            return _do_budget_stop("бюджет 100 %", exec_sid, round_no)
        if _stop_requested(worktree):
            _set_stage(store, task_id, "stopped", round_no, "stop requested")
            return "stopped"
        _set_stage(store, task_id, f"review r{round_no}", round_no, "ревью")
        diff_text = _diff_text(worktree, base_sha)
        gate_for_prompt = gate if gate is not None else {"commit": True, "scope": [],
                                                         "tests": {"ok": True, "output": ""}}

        import threading as _th

        transient_review_errs: dict[str, str] = {}
        transient_lock = _th.Lock()

        def _run_one(item) -> str | None:
            name, runner = item
            over1, _, _ = _over_budget(round_no)
            if over1:
                return None
            prompt = prompts.review_prompt(rules_text, card_text, diff_text,
                                           gate_for_prompt, round_no, task_blind)
            # Каждому ревьюеру свой файл (как PanelReviewer): иначе все пишут
            # в общий review_rN.json — гонка, лишний resume и потеря вердикта.
            per_file = f"review_r{round_no}_{name}.json"
            prompt = prompt.replace(f"review_r{round_no}.json", per_file)
            log = str(Path(worktree) / ".agent" / f"reviewer_r{round_no}_{name}.log")
            # H13: ревьюер — новой сессией, повторы при TransientError.
            try:
                try:
                    rsid = _with_transient_retry(
                        store, task_id,
                        lambda: runner.start(prompt, worktree, log),
                        retry_max, retry_base)
                except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                    if _is_transient_exc(e):
                        try:
                            with transient_lock:
                                transient_review_errs[name] = str(e) or "сбой opencode"
                        except (ValueError, AttributeError):
                            pass
                    return None
            except (OSError, RuntimeError, subprocess.SubprocessError):
                return None
            # Сессия линкуется сразу, как только id известен (до resume).
            try:
                rtool, _rm = _runner_tool_model(runner, {"executor": name})
                store.link_session(rsid, rtool, task_id, "reviewer", round_no,
                                   str(getattr(runner, "model", name) or name).split("/")[-1])
            except (OSError, sqlite3.Error, ValueError):
                pass
            # Один повтор не записавшему валидный JSON (как PanelReviewer).
            # H13: добивка — первая попытка та же сессия, повторы — новой.
            own = Path(worktree) / ".agent" / per_file
            if not _review_file_valid(own):
                fix_text = REVIEW_FIX_TEXT.replace("review_rN.json", per_file)

                try:
                    try:
                        rsid2 = _review_fix_with_retry(
                            store, task_id, runner, fix_text, worktree,
                            log, per_file, rsid, retry_max, retry_base)
                    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                        if _is_transient_exc(e):
                            try:
                                with transient_lock:
                                    transient_review_errs[name] = str(e) or "сбой opencode"
                            except (ValueError, AttributeError):
                                pass
                        return rsid
                    try:
                        rtool, _rm = _runner_tool_model(runner, {"executor": name})
                        store.link_session(rsid2, rtool, task_id, "reviewer", round_no,
                                           str(getattr(runner, "model", name) or name
                                               ).split("/")[-1])
                    except (OSError, sqlite3.Error, ValueError):
                        pass
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    pass
            return rsid

        if reviewers:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(reviewers))) as pool:
                list(pool.map(_run_one, list(reviewers.items())))
        over, _, _ = _over_budget(round_no)
        if over:
            return _do_budget_stop("бюджет 100 %", exec_sid, round_no)
        # H13 п.2: любой ревьюер исчерпал повторы сети/сервера — arbiter
        # с честной причиной, а не stub-approve «не дал JSON» + ready.
        if transient_review_errs:
            try:
                first = sorted(transient_review_errs.items())[0][1]
            except (ValueError, AttributeError, IndexError):
                first = "сбой opencode"
            _set_stage(store, task_id, "arbiter", round_no,
                       _transient_reason(first))
            return "arbiter"
        # Молчавших (не транзиентно) видно в findings (low), вердикт — по ответившим.
        if reviewers:
            valid_names = {p.stem.removeprefix(f"review_r{round_no}_")
                           for p in (Path(worktree) / ".agent").glob(f"review_r{round_no}_*.json")
                           if _review_file_valid(p)}
            if valid_names and len(valid_names) < len(reviewers):
                for name in sorted(set(reviewers) - valid_names):
                    if name in transient_review_errs:
                        continue
                    try:
                        (Path(worktree) / ".agent" / f"review_r{round_no}_{name}.json"
                         ).write_text(json.dumps({
                             "verdict": "approve",
                             "findings": [{"severity": "low", "file": "", "line": 0,
                                           "issue": "ревьюер не дал валидный JSON",
                                           "fix": ""}],
                         }, ensure_ascii=False), encoding="utf-8")
                    except OSError:
                        continue

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
        gate_green = gate is not None and not gate.errors
        if decision == "ready":
            if not gate_green:
                # approve при красных воротах — не ready (как run_task:
                # approve→changes «ворота не пройдены»). Находка для
                # следующего круга перечитается с диска (fix rN+1),
                # сюда её класть не нужно.
                gate_tail = "; ".join(getattr(gate, "errors", []) or [])[:300] \
                    if gate is not None else "нет ворот"
                if round_no >= max(1, int(rounds)):
                    _set_stage(store, task_id, "arbiter", round_no,
                                f"approve при красных воротах: {gate_tail}"[:500])
                    return "arbiter"
                _set_stage(store, task_id, f"review r{round_no}", round_no,
                            "approve→changes(ворота)")
                continue
            _set_stage(store, task_id, "ready", round_no, "панель approve")
            return "ready"
        if decision == "arbiter":
            reason = ",".join(r.verdict for r in reviews) or "панель молчит"
            _set_stage(store, task_id, "arbiter", round_no, reason[:500])
            return "arbiter"
        # next → следующий круг. verdict() на последнем круге сам отдаёт
        # arbiter, а конец цикла ниже — страховка, если вердикт изменится.
        _set_stage(store, task_id, f"review r{round_no}", round_no, "changes → круг")
        continue
    _set_stage(store, task_id, "arbiter", int(rounds), "круги кончились")
    return "arbiter"
