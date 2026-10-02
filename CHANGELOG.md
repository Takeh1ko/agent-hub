# Changelog

## 3.0.0 — 2026-10-03

First public release.

- Package `ahub` on PyPI; command `ahub`; Python 3.11+.
- `ahub setup` wizard (language, project, providers, models, OS service, Claude Code skill, Telegram) and
  `ahub doctor` (what is wrong and how to fix it).
- English and Russian interface; stable event codes for Claude (`DONE`, `DECISION`, `ERROR`, `OWNER`, `ANSWER`,
  `ALARM`); worker prompts in English, answers in the hub language.
- Providers: opencode and Google Antigravity (`agy`, Gemini).
- macOS support (psutil, launchd); `ahub service start|stop` without an OS service; Windows via WSL2.
- Telegram is optional (`ahub[telegram]`); settings live in the hub config.
- Reviewer that does not hand in a verdict gets one retry in the same session; the observer only judges log
  entries inside its check window.

Versions 1 and 2 were private.
