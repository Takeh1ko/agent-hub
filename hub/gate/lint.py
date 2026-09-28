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


@dataclass
class LintResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


def strip_arbiter(text: str) -> str:
    """Вырезать раздел «Решения арбитра» до следующего заголовка того же/высшего уровня."""
    lines = text.splitlines()
    # Индексы строк-заголовков вида `#{1,6} ...`.
    head_re = re.compile(r"^(#{1,6})\s.*$")
    # Найти старт: заголовок с фразой «Решения арбитра».
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        m = head_re.match(lines[i])
        if m and "Решения арбитра" in lines[i]:
            level = len(m.group(1))
            i += 1
            # Пропускать до следующего заголовка того же/высшего уровня или конца.
            while i < n:
                m2 = head_re.match(lines[i])
                if m2 and len(m2.group(1)) <= level:
                    break
                i += 1
            continue
        out.append(lines[i])
        i += 1
    res = "\n".join(out)
    # Сохранить концевой перевод строки как в исходнике.
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
        # Строка вида `**Цель.** ...` уже покрыта; запасной путь —
        # имя в начале строки (первые 60 симв.) с `**` где-то в строке.
        if name in s[:60] and "**" in s:
            return idx
    return None


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """Границы раздела [start, end) в индексах строк (0..), включая строку заголовка."""
    start_no = _header_line_no(lines, name)
    if start_no is None:
        return None
    start = start_no - 1  # включая строку заголовка: тело часто на той же строке
    body_from = start_no  # поиск следующего заголовка — только после текущего
    end = len(lines)
    # Конец — следующий обязательный раздел после старта.
    for other in REQUIRED_SECTIONS:
        if other == name:
            continue
        no = _header_line_no(lines[body_from:], other)
        # _header_line_no считает от 1 внутри среза; пересчитать.
        if no is not None:
            # Найти реальный номер: ищем первый такой заголовок после start.
            for idx in range(body_from, len(lines)):
                if _header_line_no([lines[idx]], other) is not None:
                    # Проверить что это именно заголовок (функция выше вернёт 1).
                    # Убедиться что idx >= start.
                    end = min(end, idx)
                    break
    # Также границей может быть markdown-заголовок «Решения арбитра» —
    # он не обязательный, но отделяет тело последнего раздела.
    for idx in range(body_from, len(lines)):
        if re.match(r"^#{1,6}\s", lines[idx]) and "Решения арбитра" in lines[idx]:
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


def _can_change_globs(section: str) -> list[str]:
    """Паттерны из «Можно менять»: содержимое `...` без пробелов внутри."""
    out: list[str] = []
    for m in re.finditer(r"`([^`]+)`", section):
        cand = m.group(1).strip()
        if not cand or any(c.isspace() for c in cand):
            continue
        # Пропустить код (`register`, `ProjectConfig`), оставить пути/glob'ы.
        if "/" not in cand and cand not in (".hub.toml", "pyproject.toml"):
            continue
        out.append(cand)
    return out


def _looks_like_concrete_path(cand: str) -> bool:
    """Конкретный путь (не glob/плейсхолдер) для проверки существования."""
    if not cand:
        return False
    if any(ch in cand for ch in ("*", "?", "[", "<", ">", "|")):
        return False
    if any(c.isspace() for c in cand):
        return False
    if "/" in cand:
        return True
    # Одиночные файлы в корне проекта.
    if cand in (".hub.toml", "pyproject.toml"):
        return True
    if re.match(r"^[\w.\-]+\.(md|py|toml|sql|json|sh)$", cand):
        return True
    return False


def _read_paths(section: str) -> list[str]:
    """Пути из «Прочитать»: `...` + голые `docs/...`, `hub/...`, `/abs/...`."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(cand: str) -> None:
        cand = cand.strip().strip(".,;:!?()[]\"'").rstrip(".,;:!?()")
        # Отрезать скобки/запятые на конце (`docs/spec.md (§7` → `docs/spec.md`).
        cand = re.split(r"\s", cand)[0] if cand else ""
        cand = cand.strip(".,;:!?()[]\"'")
        if not _looks_like_concrete_path(cand):
            return
        if cand not in seen:
            seen.add(cand)
            found.append(cand)

    for m in re.finditer(r"`([^`]+)`", section):
        _add(m.group(1).strip())
    # Голые пути с `/`: искать вне бэктиков, чтобы не резать `docs/a/b.md` на части.
    no_tick = re.sub(r"`[^`]*`", " ", section)
    for m in re.finditer(r"(?<![\w/`])(/?(?:[\w.\-]+/)+[\w.\-]+(?:\.[\w]+)?)", no_tick):
        _add(m.group(1))
    return found


def _pytest_nodes(section: str) -> list[str]:
    """Pytest-ноды из «Приёмки»: аргументы команд `pytest ...`."""
    cmds: list[str] = []
    for m in re.finditer(r"`([^`]*pytest[^`]*)`", section):
        cmds.append(m.group(1))
    for m in re.finditer(r"(?m)^[^\n`]*\bpytest\s+([^\n`]+)", section):
        # Не дублировать то, что уже взято из бэктиков той же строки.
        frag = m.group(0)
        if "pytest" in frag and frag not in cmds:
            cmds.append(frag)
    nodes: list[str] = []
    for cmd in cmds:
        # Оставить хвост после слова pytest.
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
            # Нода: путь/каталог или `файл::тест`.
            if "/" in t or t.endswith(".py") or "::" in t or t.startswith("tests"):
                # Отрезать висячую пунктуацию.
                t = t.strip(".,;:!?()[]\"'")
                if t:
                    nodes.append(t)
    # Дедуп с сохранением порядка.
    seen: set[str] = set()
    uniq: list[str] = []
    for nd in nodes:
        if nd not in seen:
            seen.add(nd)
            uniq.append(nd)
    # Запасной путь: голые `tests/...` без слова pytest.
    if not uniq:
        for m in re.finditer(r"(tests/[^\s`'\",;:!?()]+)", section):
            cand = m.group(1).strip(".,;:!?()[]\"'")
            if cand and cand not in seen:
                seen.add(cand)
                uniq.append(cand)
    return uniq


def _resolve(p: str, project: ProjectConfig, card_parent: Path) -> Path:
    pp = Path(p)
    if pp.is_absolute():
        return pp
    root = project.root.strip() if project.root else ""
    if root:
        return Path(root) / p
    return card_parent / p


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
    # Дальнейшие проверки — по имеющимся разделам.
    # 2. «Можно менять» ⊆ allowed_paths.
    if "Можно менять" not in missing:
        section = _section_text(lines, "Можно менять")
        globs = _can_change_globs(section)
        if not globs:
            errors.append(f"{card}: «Можно менять» без путей")
        for g in globs:
            ok_glob = any(
                fnmatch.fnmatch(g, pat) or fnmatch.fnmatch(g.lstrip("./"), pat)
                for pat in (project.allowed_paths or [])
            )
            if not ok_glob:
                ln = _line_of(lines, g, card)
                if ln is not None:
                    errors.append(f"{card}:{ln}: glob «{g}» вне allowed_paths")
                else:
                    errors.append(f"{card}: glob «{g}» вне allowed_paths")
    # 3. Пути из «Прочитать» существуют.
    if "Прочитать" not in missing:
        section = _section_text(lines, "Прочитать")
        for p in _read_paths(section):
            rp = _resolve(p, project, card.parent)
            if not rp.exists():
                ln = _line_of(lines, p, card)
                if ln is not None:
                    errors.append(f"{card}:{ln}: нет пути «{p}»")
                else:
                    errors.append(f"{card}: нет пути «{p}»")
    # 4. Приёмка: есть ноды и они собираются.
    if "Приёмка" not in missing:
        section = _section_text(lines, "Приёмка")
        nodes = _pytest_nodes(section)
        if not nodes:
            errors.append(f"{card}: «Приёмка» без pytest-нод")
        else:
            root = project.root.strip() if project.root else str(card.parent)
            cwd = Path(root) if Path(root).is_dir() else card.parent
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "pytest", "--collect-only", "-q", *nodes],
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
                    ln = _line_of(lines, nodes[0], card) if nodes else None
                    msg = f"pytest --collect-only не собрал {nodes}: {one}"
                    if ln is not None:
                        errors.append(f"{card}:{ln}: {msg}")
                    else:
                        errors.append(f"{card}: {msg}")
    return LintResult(ok=not errors, errors=errors)
