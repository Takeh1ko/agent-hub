"""i18n step 8: no Cyrillic in ahub/ strings outside ahub/i18n (except the allow-list).

Comments and docstrings are not checked (separate task).
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

_CYR = re.compile(r"[а-яА-ЯёЁ]")

# (file suffix, substring in the token, reason) — any other Cyrillic is forbidden.
ALLOW: list[tuple[str, str, str]] = [
    ("ahub/time.py", "", "разбор русского ввода времени (сегодня/вчера/единицы, оба языка всегда)"),
    ("ahub/views.py", "Суть", "регулярка русских заголовков отчёта (views._SUT понимает Суть/Summary)"),
    ("ahub/review.py", "Решени", "регулярка русских заголовков отчёта (review._ARBITER понимает Решения арбитра/оркестратора)"),
    ("ahub/prompts.py", "", "заголовки в prompts.py при lang()=='ru' (Суть, Решение арбитра, Указания оркестратора, «готово»/«заблокировано»)"),
    ("ahub/drafts.py", "разведка", "приём русских значений kind в drafts.py (kind_alias: разведка→scout)"),
    ("ahub/drafts.py", "код", "приём русских значений kind в drafts.py (kind_alias: код→code)"),
    ("ahub/drafts.py", "рутина", "приём русских значений kind в drafts.py (kind_alias: рутина→routine)"),
    ("ahub/drafts.py", "ревью", "приём русских значений kind в drafts.py (kind_alias: ревью→review)"),
    ("ahub/tg/core.py", "по ", "русские префиксы проекта в tg/core.split_project («по <проект>:»)"),
]


def _doc_lines(src: str) -> set[int]:
    """Docstring lines: a string expression first in a module/class/function."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    lines: set[int] = set()

    def _mark(body: list[ast.stmt]) -> None:
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            n = body[0].value
            for ln in range(n.lineno, (n.end_lineno or n.lineno) + 1):
                lines.add(ln)

    _mark(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _mark(node.body)
    return lines


def _string_tokens(src: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    toks = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok in toks:
        name = tokenize.tok_name.get(tok.type, "")
        if "STRING" in name or tok.type == tokenize.STRING:
            out.append((tok.start[0], tok.string))
        elif name == "FSTRING_MIDDLE":
            out.append((tok.start[0], tok.string))
    return out


def _allowed(path: str, token: str) -> bool:
    for suffix, sub, _reason in ALLOW:
        if path.endswith(suffix) and sub in token:
            return True
    return False


def test_no_cyrillic_outside_i18n():
    root = Path(__file__).resolve().parents[1] / "ahub"
    bad: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root.parent).as_posix()
        if rel.startswith("ahub/i18n/"):
            continue
        src = path.read_text(encoding="utf-8")
        docs = _doc_lines(src)
        # map token lines → skip the docstring lines
        try:
            tree = ast.parse(src)
        except SyntaxError:
            tree = None
        _ = tree
        for lineno, tok in _string_tokens(src):
            if not _CYR.search(tok):
                continue
            # the whole token is inside a docstring — skip it
            # (rough: the token's start line is a docstring line)
            if lineno in docs:
                continue
            if _allowed(rel, tok):
                continue
            bad.append(f"{rel}:{lineno}: {tok[:120]!r}")
    assert not bad, "кириллица в строках ahub/ вне allow-листа:\n" + "\n".join(bad)


def test_allow_list_has_reasons():
    assert ALLOW and all(r.strip() for _, _, r in ALLOW)
