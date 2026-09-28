"""Линт карточки задачи (§7 spec). Чистые проверки, без глобального состояния."""

from __future__ import annotations

import fnmatch
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from hub.config import ProjectConfig

# Обязательные разделы карточки (§7, словарь §2).
REQUIRED_SECTIONS = [
    "Цель",
    "Прочитать",
    "Можно менять",
    "Интерфейс",
    "Приёмка",
    "Нельзя",
    "Сеть",
    "Исполнитель",
    "Уровень",
    "Коммит",
]

# Размер карточки ≤ 12 КБ.
MAX_CARD_BYTES = 12 * 1024

_CYR = re.compile(r"[а-яА-ЯёЁ]")


@dataclass
class LintResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def strip_arbiter(text: str) -> str:
    """Вырезать раздел «Решения арбитра» до следующего заголовка того же/высшего уровня."""
    lines = text.splitlines()
    head_re = re.compile(r"^(#{1,6})\s*\S.*$")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        s = lines[i].strip()
        m = head_re.match(lines[i])
        # Markdown-заголовок с фразой (с пробелом и без: `#Решения`, `## Решения арбитра`).
        if m and "Решения арбитра" in lines[i]:
            level = len(m.group(1))
            i += 1
            while i < n:
                m2 = head_re.match(lines[i])
                if m2 and len(m2.group(1)) <= level:
                    break
                # Жирный заголовок тоже заканчивает markdown-раздел.
                if lines[i].strip().startswith("**"):
                    break
                i += 1
            continue
        # Жирный заголовок `**Решения арбитра...` — стиль остальных разделов карточки.
        if s.startswith("**") and "Решения арбитра" in s:
            i += 1
            while i < n:
                t = lines[i].strip()
                if head_re.match(lines[i]):
                    break
                if t.startswith("**"):
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    res = "\n".join(out)
    if text.endswith("\n"):
        res += "\n"
    return res


def _header_line_no(lines: list[str], name: str) -> int | None:
    """Номер строки (1..) заголовка раздела или None."""
    for idx, line in enumerate(lines, 1):
        s = line.strip()
        if name not in s:
            continue
        # Заголовок: markdown `#` или жирное `**Имя`.
        if s.startswith("#") or s.startswith("**") or f"**{name}" in s:
            return idx
        if name in s[:60] and "**" in s:
            return idx
    return None


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """Границы раздела [start, end) в индексах строк (0..), включая строку заголовка."""
    start_no = _header_line_no(lines, name)
    if start_no is None:
        return None
    start = start_no - 1
    body_from = start_no
    end = len(lines)
    for other in REQUIRED_SECTIONS:
        if other == name:
            continue
        no = _header_line_no(lines[body_from:], other)
        if no is not None:
            for idx in range(body_from, len(lines)):
                if _header_line_no([lines[idx]], other) is not None:
                    end = min(end, idx)
                    break
    for idx in range(body_from, len(lines)):
        if re.match(r"^#{1,6}\s*\S", lines[idx]) and "Решения арбитра" in lines[idx]:
            end = min(end, idx)
            break
        t = lines[idx].strip()
        if t.startswith("**") and "Решения арбитра" in t:
            end = min(end, idx)
            break
    return (start, end)


def _section_text(lines: list[str], name: str) -> str:
    b = _section_bounds(lines, name)
    if b is None:
        return ""
    return "\n".join(lines[b[0]:b[1]])


def _line_of(lines: list[str], needle: str, path: Path) -> int | None:
    """Первая строка с подстрокой needle (для `path:line:`)."""
    for idx, line in enumerate(lines, 1):
        if needle in line:
            return idx
    return None


def _line_of_in_section(
    lines: list[str], bounds: tuple[int, int] | None, needle: str
) -> int | None:
    """Первая строка с needle только внутри границ секции."""
    if bounds is None:
        return None
    start, end = bounds
    for idx in range(start, end):
        if needle in lines[idx]:
            return idx + 1
    return None


def _can_change_globs(section: str) -> list[str]:
    """Паттерны из «Можно менять»: бэктики + голые токены + glob-символы."""
    out: list[str] = []
    seen: set[str] = set()

    def _add(cand: str) -> None:
        cand = cand.strip().strip("\"'(),;")
        if not cand or any(c.isspace() for c in cand):
            return
        if _CYR.search(cand):
            return  # проза, не путь
        has_glob = any(c in cand for c in ("*", "?", "["))
        if has_glob:
            # Glob без `/` — только файловый (`*.py`); `[project.scripts]` — не путь.
            if "/" not in cand and cand not in (".hub.toml", "pyproject.toml"):
                if ("*" not in cand and "?" not in cand) or "." not in cand:
                    return
                if not re.match(r"^[A-Za-z0-9_.\-/*?\[\]]+$", cand):
                    return
        elif "/" not in cand and cand not in (".hub.toml", "pyproject.toml"):
            return  # код (`register`, `ProjectConfig`), не путь
        if cand not in seen:
            seen.add(cand)
            out.append(cand)

    for m in re.finditer(r"`([^`]+)`", section):
        _add(m.group(1).strip())
    no_tick = re.sub(r"`[^`]*`", " ", section)
    # Голые пути с `/` (вне бэктиков).
    for m in re.finditer(r"(?<![\w/`~])(/?(?:[\w.\-]+/)+[\w.\-]+(?:\.[\w]+)?)", no_tick):
        _add(m.group(1))
    # Голые glob-токены без `/` (например `*.py`).
    for m in re.finditer(r"[^\s`'\",;:!?()]+[*?\[\]][^\s`'\",;:!?()]*", no_tick):
        _add(m.group(0))
    # Одиночные файлы без `/`.
    for name in (".hub.toml", "pyproject.toml"):
        if name in no_tick and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def _looks_like_concrete_path(cand: str) -> bool:
    """Конкретный путь (не glob/плейсхолдер, не проза) для проверки существования."""
    if not cand:
        return False
    if any(ch in cand for ch in ("*", "?", "[", "<", ">", "|")):
        return False
    if "::" in cand:
        return False
    if cand.startswith("~") or cand.startswith("$"):
        return False
    if any(c.isspace() for c in cand):
        return False
    if _CYR.search(cand):
        # Проза со слэшами (`событие/вопрос`) — не путь; файл с кириллицей
        # (`hub/нет-такого.py`, `docs/нет.md`) — проверять существованием.
        if not re.search(r"\.(md|py|toml|sql|json|sh)$", cand):
            return False
    if "/" in cand:
        return True
    if cand in (".hub.toml", "pyproject.toml"):
        return True
    if re.match(r"^[\w.\-]+\.(md|py|toml|sql|json|sh)$", cand):
        return True
    return False


def _read_paths(section: str, project_root: Path | None) -> list[str]:
    """Пути из «Прочитать»: `...` + голые `docs/...`, `hub/...`, `/abs/...`."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(cand: str) -> None:
        cand = cand.strip()
        if not cand:
            return
        # Взять первый токен (отрезать `docs/spec.md (§7` → `docs/spec.md`).
        cand = re.split(r"\s", cand)[0] if cand else ""
        # Резать только кавычки/скобки и хвостовую пунктуацию; ведущую `.` беречь.
        cand = cand.lstrip("\"'([")
        cand = cand.rstrip("\"')].,;:!?")
        if not _looks_like_concrete_path(cand):
            return
        # Для путей с `/`: первый сегмент должен существовать в project.root,
        # иначе это проза (`Store.add/a`, `Snapshot.to/a`) или чужое (`agent/...`).
        if "/" in cand and not Path(cand).is_absolute() and project_root is not None:
            first = Path(cand).parts[0] if Path(cand).parts else ""
            if first and not (project_root / first).exists():
                return
        if cand not in seen:
            seen.add(cand)
            found.append(cand)

    for m in re.finditer(r"`([^`]+)`", section):
        _add(m.group(1).strip())
    no_tick = re.sub(r"`[^`]*`", " ", section)
    for m in re.finditer(r"(?<![\w/`~])(/?(?:[\w.\-]+/)+[\w.\-]+(?:\.[\w]+)?)", no_tick):
        _add(m.group(1))
    return found


def _is_valid_node(t: str) -> bool:
    """Строгая нода: tests/..., *.py или ...::... без мусора."""
    if not t or _CYR.search(t):
        return False
    if t.startswith("~") or t.startswith("$"):
        return False
    if ".." in Path(t).parts:
        return False
    if t.startswith("tests/") or t in ("tests", "tests/"):
        return True
    if t.endswith(".py"):
        return True
    if "::" in t:
        return True
    return False


def _pytest_nodes(section: str) -> list[str]:
    """Pytest-ноды только из команд `pytest ...`. Без fallback."""
    cmds: list[str] = []
    for m in re.finditer(r"(?m)`([^`\n]*pytest[^`\n]*)`", section):
        cmds.append(m.group(1))
    for m in re.finditer(r"(?m)^[^\n`]*\bpytest\s+([^\n`]+)", section):
        frag = m.group(0)
        if "pytest" in frag and frag not in cmds:
            cmds.append(frag)
    nodes: list[str] = []
    for cmd in cmds:
        tail = cmd[cmd.find("pytest") + len("pytest"):]
        try:
            toks = shlex.split(tail)
        except ValueError:
            toks = tail.split()
        for t in toks:
            t = t.strip().strip("\"'")
            if not t or t.startswith("-"):
                continue
            if t in ("pytest", "-q", "--collect-only", "-qq"):
                continue
            if "=" in t and "/" not in t and "::" not in t:
                continue
            t = t.strip(".,;:!?()[]\"'")
            if not t:
                continue
            if _is_valid_node(t):
                nodes.append(t)
    seen: set[str] = set()
    uniq: list[str] = []
    for nd in nodes:
        if nd not in seen:
            seen.add(nd)
            uniq.append(nd)
    return uniq


def _resolve(p: str, project: ProjectConfig, card_parent: Path) -> Path:
    pp = Path(p)
    if pp.is_absolute():
        return pp
    root = project.root.strip() if project.root else ""
    if root:
        return Path(root) / p
    return card_parent / p


def _collect_cwd(card: Path, project: ProjectConfig) -> Path:
    """Cwd для collect: git-корень карточки, иначе project.root, иначе папка карточки."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(card.parent),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            p = Path(r.stdout.strip())
            if p.is_dir():
                return p
    except (OSError, subprocess.TimeoutExpired):
        pass
    root = project.root.strip() if project.root else ""
    if root and Path(root).is_dir():
        return Path(root)
    return card.parent


def _collect_python(project: ProjectConfig) -> tuple[str | None, str | None]:
    """Питон проекта; (путь, ошибка). Несуществующий заданный питон — ошибка."""
    python = (project.python or "").strip()
    if python:
        if Path(python).exists():
            return python, None
        return None, f"питон проекта не найден: {python}"
    return sys.executable, None


def lint_card(path: Path, project: ProjectConfig) -> LintResult:
    """Проверить карточку по §7. Ошибки вида `path:line: текст` или `path: текст`."""
    card = Path(path)
    errors: list[str] = []
    try:
        text = card.read_text(encoding="utf-8")
    except OSError as e:
        return LintResult(ok=False, errors=[f"{card}: не читается: {e}"])
    try:
        size = card.stat().st_size
    except OSError:
        size = len(text.encode("utf-8"))
    if size > MAX_CARD_BYTES:
        errors.append(f"{card}: размер {size} байт > 12 КБ")
    lines = text.splitlines()

    # 1. Обязательные разделы.
    missing: list[str] = []
    for name in REQUIRED_SECTIONS:
        if _header_line_no(lines, name) is None:
            missing.append(name)
            errors.append(f"{card}: нет раздела «{name}»")
    # 2. «Можно менять» ⊆ allowed_paths.
    if "Можно менять" not in missing:
        section = _section_text(lines, "Можно менять")
        globs = _can_change_globs(section)
        if not globs:
            errors.append(f"{card}: «Можно менять» без путей")
        for g in globs:
            # Выход наружу — сразу вне allowed_paths (lstrip тут запрещён).
            if Path(g).is_absolute() or ".." in Path(g).parts:
                ln = _line_of(lines, g, card)
                msg = f"glob «{g}» вне allowed_paths"
                errors.append(f"{card}:{ln}: {msg}" if ln is not None else f"{card}: {msg}")
                continue
            norm = g.removeprefix("./")
            ok_glob = any(
                fnmatch.fnmatch(norm, pat)
                for pat in (project.allowed_paths or [])
            )
            if not ok_glob:
                ln = _line_of(lines, g, card)
                msg = f"glob «{g}» вне allowed_paths"
                errors.append(f"{card}:{ln}: {msg}" if ln is not None else f"{card}: {msg}")
    # 3. Пути из «Прочитать» существуют.
    if "Прочитать" not in missing:
        section = _section_text(lines, "Прочитать")
        root = Path(project.root) if (project.root or "").strip() else None
        if root is not None and not root.is_dir():
            root = None
        for p in _read_paths(section, root):
            rp = _resolve(p, project, card.parent)
            if not rp.exists():
                ln = _line_of(lines, p, card)
                msg = f"нет пути «{p}»"
                errors.append(f"{card}:{ln}: {msg}" if ln is not None else f"{card}: {msg}")
    # 4. Приёмка: есть ноды из команд pytest и они собираются.
    if "Приёмка" not in missing:
        section = _section_text(lines, "Приёмка")
        bounds = _section_bounds(lines, "Приёмка")
        nodes = _pytest_nodes(section)
        if not nodes:
            errors.append(f"{card}: «Приёмка» без pytest-нод")
        else:
            cwd = _collect_cwd(card, project)
            py, py_err = _collect_python(project)
            if py_err is not None:
                errors.append(f"{card}: {py_err}")
            else:
                assert py is not None
                try:
                    r = subprocess.run(
                        [py, "-m", "pytest", "--collect-only", "-q", *nodes],
                        cwd=str(cwd),
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                except OSError as e:
                    errors.append(f"{card}: pytest не запустился: {e}")
                except subprocess.TimeoutExpired:
                    errors.append(f"{card}: pytest --collect-only: таймаут")
                else:
                    if r.returncode != 0:
                        tail = (r.stdout + "\n" + r.stderr).strip()
                        tail = tail[-2000:] if len(tail) > 2000 else tail
                        one = tail.splitlines()[-1] if tail else f"код {r.returncode}"
                        ln = _line_of_in_section(lines, bounds, nodes[0]) if nodes else None
                        msg = f"pytest --collect-only не собрал {nodes}: {one}"
                        errors.append(f"{card}:{ln}: {msg}" if ln is not None else f"{card}: {msg}")
    return LintResult(ok=not errors, errors=errors)
