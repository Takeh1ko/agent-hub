"""Вердикт панели ревью: ready / next / arbiter. Чистая функция."""

from __future__ import annotations

from dataclasses import dataclass

VALID_DISPUTE_BODY = 50


@dataclass
class Review:
    verdict: str  # approve | changes | dispute
    file: str = ""
    line: int = 0
    body: str = ""


def _effective(v: Review) -> str:
    if v.verdict == "approve":
        return "approve"
    if v.verdict == "changes":
        return "changes"
    if v.verdict == "dispute":
        if v.file and v.line and len(v.body or "") >= VALID_DISPUTE_BODY:
            return "dispute"
        return "changes"
    return "changes"


def verdict(reviews: list[Review], round: int, max_rounds: int = 2) -> str:
    """Свести отзывы панели к ready/next/arbiter.

    dispute без file:line или с телом короче 50 символов = changes.
    """
    eff = [_effective(r) for r in reviews]
    if eff and all(v == "approve" for v in eff):
        return "ready"
    if any(v == "changes" for v in eff):
        return "next" if round < max_rounds else "arbiter"
    if not eff:
        return "next" if round < max_rounds else "arbiter"
    return "arbiter"
