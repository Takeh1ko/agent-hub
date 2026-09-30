"""Черновики карточек от владельца (H15): текст → карточка модели → запуск по кнопке.

Модель ТОЛЬКО ЧИТАЕТ код проекта и пишет карточку в
`<project>/docs/tasks/_drafts/<ID>-<slug>.md` по промпту
`docs/prompts/draft_card.md`. Проверка — `lint_card`; при ошибках один
повтор с текстом ошибок в промпте. Сбой сети/сервера: `TransientError`
(если есть, как H13) — повторы, иначе `failed`.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import time
import tomllib
from pathlib import Path

DRAFT_STATUSES = ("drafting", "ready", "failed", "started", "cancelled")
DRAFT_SOURCES = ("tg", "top", "cli")

PROMPT_REL = Path("docs/prompts/draft_card.md")

# Пауза повтора при TransientError — через свою функцию, чтобы тесты
# подменяли только её (как H13, круг 3, п.3).
_retry_sleep = time.sleep

_TRANSIENT_HINTS = (
    "unexpected server error",
    "cannot connect to api",
    "unable to connect",
    "econnrefused",
    "etimedout",
    "socket hang up",
    "429",
    "status 5",
)


def _is_transient(exc: BaseException) -> bool:
    """Транзиентный сбой сети/сервера: класс TransientError или текст-подсказка."""
    if type(exc).__name__ == "TransientError":
        return True
    try:
        text = str(exc or "").lower()
    except Exception:
        return False
    return any(h in text for h in _TRANSIENT_HINTS)


def slugify(text: str) -> str:
    """Короткий слаг для имени файла черновика (транслит, дефисы, ≤30)."""
    table = str.maketrans({
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    })
    s = str(text or "").lower().translate(table)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        return "task"
    parts = s.split("-")[:5]
    out = "-".join(parts)[:30].strip("-")
    return out or "task"


def next_owner_id(root: str | Path) -> str:
    """Следующий свободный O<N>: максимум по docs/tasks/*.md и _drafts/*.md."""
    base = Path(root)
    seen: list[int] = []
    for d in (base / "docs" / "tasks", base / "docs" / "tasks" / "_drafts"):
        try:
            files = list(d.glob("*.md"))
        except OSError:
            continue
        for f in files:
            m = re.match(r"^O(\d+)\b", f.name)
            if m:
                try:
                    seen.append(int(m.group(1)))
                except (TypeError, ValueError):
                    continue
    return f"O{(max(seen) + 1) if seen else 1}"


def parse_card_id(card_text: str) -> str | None:
    """ID из заголовка `# ID — …` (первая строка с `#`)."""
    for line in str(card_text or "").splitlines():
        s = line.strip()
        if not s.startswith("#"):
            continue
        m = re.match(r"^#{1,6}\s*([A-Za-zА-Яа-я0-9]+)\b", s)
        if m:
            return m.group(1)
        return None
    return None


def _section_simple(card_text: str, name: str) -> str:
    """Значение раздела через разбор lint; фолбэк — пусто."""
    try:
        from hub.gate import lint as lint_mod

        return lint_mod._section_text(str(card_text or "").splitlines(), name)
    except (ImportError, AttributeError):
        return ""


def draft_preview(card_text: str, limit: int = 1500) -> str:
    """Короткий предпросмотр ≤ limit: название, цель, файлы, проверка, исполнитель."""
    text = str(card_text or "")
    title = ""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            title = re.sub(r"^#{1,6}\s*", "", s).strip()[:120]
            break
    goal = re.sub(r"\s+", " ", _section_simple(text, "Цель")).strip()
    # Цель — 1–2 предложения.
    parts = re.split(r"(?<=[.!?])\s+", goal)
    goal = " ".join(p for p in parts[:2] if p).strip()[:400]
    files = re.sub(r"\s+", " ", _section_simple(text, "Можно менять")).strip()[:300]
    accept = re.sub(r"\s+", " ", _section_simple(text, "Приёмка")).strip()[:300]
    executor = re.sub(r"\s+", " ", _section_simple(text, "Исполнитель")).strip()[:120]
    lines = []
    if title:
        lines.append(title)
    if goal:
        lines.append(f"Цель: {goal}")
    if files:
        lines.append(f"Файлы: {files}")
    if accept:
        lines.append(f"Проверка: {accept}")
    if executor:
        lines.append(f"Исполнитель: {executor}")
    out = "\n".join(lines).strip() or text.strip()[:500]
    if len(out) > limit:
        out = out[:limit]
    return out


def draft_model_name(project) -> str:
    """Модель для черновика: [draft] model из .hub.toml проекта, иначе muse."""
    root = str(getattr(project, "root", "") or "").strip()
    if root:
        try:
            toml = Path(root) / ".hub.toml"
            if toml.is_file():
                data = tomllib.loads(toml.read_text(encoding="utf-8"))
                draft = data.get("draft", {}) or {}
                model = str(draft.get("model", "") or "").strip()
                if model:
                    return model
        except (OSError, tomllib.TOMLDecodeError):
            pass
    return "muse"


def make_runner_for_project(project):
    """Раннер модели для черновика по [draft] model (по умолчанию muse)."""
    from hub.pipeline.runners import make_runner

    return make_runner(draft_model_name(project))


def _load_prompt_template() -> str:
    """Шаблон промпта: рядом с hub/ или в cwd (тесты — по месту пакета)."""
    here = Path(__file__).resolve()
    for base in (here.parents[1], Path.cwd()):
        cand = base / PROMPT_REL
        try:
            if cand.is_file():
                return cand.read_text(encoding="utf-8")
        except OSError:
            continue
    return (
        "Напиши карточку задачи по тексту владельца: {{OWNER_TEXT}}. "
        "Файл карточки: {{CARD_PATH}}. ID: {{CARD_ID}}. {{LINT_ERRORS}}"
    )


def build_draft_prompt(template: str, owner_text: str, project,
                        card_id: str, card_path: Path,
                        owner_edit: str = "", lint_errors: str = "") -> str:
    """Подставить значения в шаблон (плейсхолдеры {{...}}, не format)."""
    root = str(getattr(project, "root", "") or "")
    name = str(getattr(project, "name", "") or "")
    lint_block = ""
    if lint_errors.strip():
        lint_block = (
            "Линтер отклонил прошлый вариант, исправь эти ошибки:\n"
            + lint_errors.strip()
        )
    return (str(template)
            .replace("{{OWNER_TEXT}}", str(owner_text or "").strip())
            .replace("{{OWNER_EDIT}}", str(owner_edit or "").strip())
            .replace("{{PROJECT_NAME}}", name)
            .replace("{{PROJECT_ROOT}}", root)
            .replace("{{CARD_PATH}}", str(card_path))
            .replace("{{CARD_ID}}", str(card_id))
            .replace("{{LINT_ERRORS}}", lint_block))


def _drafts_dir(project) -> Path:
    root = str(getattr(project, "root", "") or "").strip()
    return Path(root) / "docs" / "tasks" / "_drafts"


def _suggest_path(project, owner_text: str) -> tuple[str, Path]:
    """Пара (card_id, путь черновика): O<N> + слаг, уникально на диске."""
    card_id = next_owner_id(str(getattr(project, "root", "") or "."))
    slug = slugify(owner_text)
    d = _drafts_dir(project)
    cand = d / f"{card_id}-{slug}.md"
    n = 2
    try:
        while cand.exists():
            cand = d / f"{card_id}-{slug}-{n}.md"
            n += 1
    except OSError:
        pass
    return card_id, cand


def _call_runner(runner, prompt: str, cwd: str):
    """Один вызов модели. Возвращает ответ раннера (обычно session id).

    TransientError пробрасывается для повтора выше."""
    log = str(Path(cwd) / ".agent" / "draft.log")
    start = getattr(runner, "start", None)
    if not callable(start):
        raise RuntimeError("у раннера нет start(prompt, cwd)")
    try:
        return start(prompt, cwd, log)
    except TypeError:
        # Фейк с другой сигнатурой (prompt, cwd).
        return start(prompt, cwd)


def _read_result(card_path: Path, project) -> str | None:
    """Прочитать карточку: свой путь, иначе newest .md из _drafts."""
    try:
        if card_path.is_file():
            return card_path.read_text(encoding="utf-8")
    except OSError:
        pass
    d = card_path.parent
    try:
        cands = [p for p in d.glob("*.md") if p.is_file()]
    except OSError:
        return None
    if not cands:
        return None
    try:
        cands.sort(key=lambda p: p.stat().st_mtime)
    except OSError:
        pass
    try:
        return cands[-1].read_text(encoding="utf-8")
    except OSError:
        return None


def make_draft(store, project, text: str, source: str, runner,
               chat_id: int = 0, owner_edit: str = "",
               now_ms: int | None = None) -> int:
    """Текст владельца → черновик ready (модель пишет карточку, lint ok).

    Возвращает draft_id. Один повтор при ошибках линта; дважды плохая —
    failed с текстом ошибок. TransientError — до 3 повторов с паузой
    120/240/480 с (через _retry_sleep); другая ошибка раннера — failed.
    """
    if source not in DRAFT_SOURCES:
        raise ValueError(f"плохой source: {source!r}")
    body = str(text or "").strip()
    if not body:
        raise ValueError("пустой текст задачи")
    root = str(getattr(project, "root", "") or "").strip()
    if not root:
        raise ValueError("у проекта нет root")
    ts = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    draft_id = store.create_draft(
        project=str(getattr(project, "name", "") or ""),
        text=body, source=str(source), chat_id=int(chat_id or 0), now_ms=ts)
    card_id, card_path = _suggest_path(project, body)
    try:
        card_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        store.update_draft(draft_id, status="failed",
                           lint_errors=f"нет каталога черновиков: {e}")
        return draft_id
    store.update_draft(draft_id, card_path=str(card_path))
    template = _load_prompt_template()

    def _attempt(lint_errors: str = "") -> str | None:
        prompt = build_draft_prompt(template, body, project, card_id,
                                    card_path, owner_edit, lint_errors)
        pauses = (120, 240, 480)
        attempt = 0
        answered: object = None
        while True:
            try:
                answered = _call_runner(runner, prompt, root)
                break
            except Exception as e:  # noqa: BLE001 — сбой модели → failed/повтор
                if _is_transient(e) and attempt < 3:
                    try:
                        _retry_sleep(pauses[min(attempt, len(pauses) - 1)])
                    except Exception:
                        pass
                    attempt += 1
                    continue
                store.update_draft(draft_id, status="failed",
                                   lint_errors=f"раннер: {e}"[:2000])
                return None
        # Фейк в тестах может вернуть текст карточки вместо записи файла.
        if isinstance(answered, str) and answered.strip().startswith("#") \
                and "Цель" in answered:
            try:
                card_path.parent.mkdir(parents=True, exist_ok=True)
                card_path.write_text(answered, encoding="utf-8")
            except OSError:
                pass
        got = _read_result(card_path, project)
        if got is None or not got.strip():
            return None
        # Модель могла написать под другим именем — зафиксировать факт.
        try:
            if not card_path.is_file():
                card_path.write_text(got, encoding="utf-8")
        except OSError:
            pass
        return got

    from hub.gate.lint import lint_card

    first = _attempt()
    if first is None:
        row = store.get_draft(draft_id)
        if row is not None and str(row.get("status") or "") == "drafting":
            store.update_draft(draft_id, status="failed",
                               lint_errors="модель не написала карточку")
        return draft_id
    res = lint_card(card_path, project)
    if res.ok:
        store.update_draft(draft_id, status="ready", card_text=first,
                           lint_errors="")
        return draft_id
    # Один повтор с ошибками линта в промпте (новым стартом — sid черновой
    # сессии для resume не храним, повтор идёт тем же start).
    second = _attempt("\n".join(res.errors))
    if second is None:
        row = store.get_draft(draft_id)
        if row is not None and str(row.get("status") or "") == "drafting":
            store.update_draft(draft_id, status="failed",
                               lint_errors="\n".join(res.errors)[:2000])
        return draft_id
    if second is None or not second.strip():
        store.update_draft(draft_id, status="failed",
                           lint_errors="\n".join(res.errors)[:2000])
        return draft_id
    # Если фейк не перезаписал файл на повторе — проверить текст, что он вернул.
    res2 = lint_card(card_path, project)
    if res2.ok:
        store.update_draft(draft_id, status="ready", card_text=second,
                           lint_errors="")
        return draft_id
    store.update_draft(draft_id, status="failed", card_text=second,
                       lint_errors="\n".join(res2.errors)[:2000])
    return draft_id


def _run_git(cwd: str, *args: str, timeout: int = 120):
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


def _commit_card_only(project, dest: Path, task_label: str) -> tuple[bool, str]:
    """Коммит ТОЛЬКО файла карточки под замком проекта (другая грязь — мимо)."""
    import fcntl
    import os as _os

    root = str(getattr(project, "root", "") or "").strip()
    try:
        rel = str(dest.relative_to(Path(root)))
    except (ValueError, OSError):
        return False, "карточка вне корня проекта"
    lock = str(getattr(project, "test_lock", "") or "").strip() or None
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
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        except OSError as e:
            try:
                _os.close(lock_fd)
            except OSError:
                pass
            return False, f"lock-error: {e}"
    try:
        a = _run_git(root, "add", "--", rel)
        if a.returncode != 0:
            err = (a.stderr or "").strip().splitlines()
            return False, f"git add: {err[-1] if err else a.returncode}"
        c = _run_git(root, "commit", "-m",
                     f"docs(tasks): карточка {task_label} от владельца",
                     "--", rel)
        if c.returncode != 0:
            tail = ((c.stdout or "") + "\n" + (c.stderr or "")).strip().splitlines()
            return False, f"git commit: {tail[-1] if tail else c.returncode}"
        return True, ""
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                _os.close(lock_fd)
            except OSError:
                pass


def start_draft(store, draft_id: int, now_ms: int | None = None) -> str:
    """Перенести карточку _drafts → docs/tasks, коммит только её, hub start.

    Возвращает task_id. Статус черновика → started. Во входящие Claude —
    «владелец поставил задачу <ID>: <текст>, карточка <путь>».
    """
    row = store.get_draft(int(draft_id))
    if row is None:
        raise ValueError(f"нет черновика {draft_id}")
    if str(row.get("status") or "") != "ready":
        raise ValueError(f"черновик {draft_id} не ready: {row.get('status')}")
    src = Path(str(row.get("card_path") or ""))
    if not src.is_file():
        raise ValueError(f"файл черновика пропал: {src}")
    owner_text = str(row.get("text") or "")
    proj_name = str(row.get("project") or "")
    # Проект: по имени из load_projects, иначе по пути карточки.
    project = None
    try:
        from hub.config import load_project, load_projects

        for p in load_projects():
            if str(getattr(p, "name", "") or "") == proj_name and proj_name:
                project = p
                break
        if project is None:
            project = load_project(str(src.parent))
    except (FileNotFoundError, OSError):
        raise ValueError("нет проекта для черновика")
    root = str(getattr(project, "root", "") or "").strip()
    dest = Path(root) / "docs" / "tasks" / src.name
    if dest.exists():
        raise ValueError(f"карточка уже есть: {dest}")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
    except OSError as e:
        raise ValueError(f"не перенеслась карточка: {e}")
    task_label = dest.stem
    ok, msg = _commit_card_only(project, dest, task_label)
    if not ok:
        try:
            dest.unlink()
        except OSError:
            pass
        raise ValueError(msg)
    try:
        src.unlink()
    except OSError:
        pass
    # Тот же путь, что hub start (уровень/исполнитель — из карточки по конфигу).
    from types import SimpleNamespace

    from hub.commands.start import cmd_start

    args = SimpleNamespace(card=str(dest), project=root, executor=None,
                           reviewers=None, rounds=2, budget_go=None,
                           budget_usd=None, after=None, blind=False)
    rc = cmd_start(args)
    if int(rc) != 0:
        raise ValueError("hub start не поставил задачу")
    task_id = dest.stem
    ts = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    store.update_draft(int(draft_id), status="started", task_id=task_id,
                       card_path=str(dest), card_text=dest.read_text(
                           encoding="utf-8") if dest.is_file() else "")
    inbox_text = (f"владелец поставил задачу {task_id}: {owner_text}, "
                  f"карточка {dest}")
    try:
        con = sqlite3.connect(str(store.path))
        try:
            con.execute(
                "INSERT INTO inbox(ts, text, source, seen_claude)"
                " VALUES (?, ?, ?, 0)",
                (ts, inbox_text[:2000], str(row.get("source") or "cli")))
            con.commit()
        finally:
            con.close()
    except sqlite3.Error:
        pass
    try:
        store.add_event(task_id, "owner_message",
                        {"text": inbox_text[:500]})
    except (OSError, ValueError, sqlite3.Error):
        pass
    return task_id


def cancel_draft(store, draft_id: int) -> bool:
    """Отменить черновик: статус cancelled, файл черновика удалён."""
    row = store.get_draft(int(draft_id))
    if row is None:
        raise ValueError(f"нет черновика {draft_id}")
    if str(row.get("status") or "") in ("started", "cancelled"):
        store.update_draft(int(draft_id), status="cancelled")
        return True
    path = str(row.get("card_path") or "")
    if path:
        try:
            p = Path(path)
            if p.is_file():
                p.unlink()
        except OSError:
            pass
    store.update_draft(int(draft_id), status="cancelled")
    return True
