"""Live transcript of a task for `ahub top` — the readable transcript (ahub/transcript.py, the same
lines as `ahub follow`) as a growing list of lines.

Read-only and light: every refresh reads only the new bytes of the session log (transcript.Reader keeps
the offset), and nothing here knows about widgets — the screen (ahub/tui/app.py) shows the lines and
sends the keys here.
"""

from __future__ import annotations

from pathlib import Path

from ahub import providers, transcript
from ahub.i18n import t as _t
from ahub.model import ACTIVE
from ahub.providers.base import Provider
from ahub.store import Session, Store
from ahub.tui.data import PHASE

WIDTH = 120  # width the transcript is rendered for (a result line is clipped to it)
MAX_LINES = 2000  # the live transcript never grows past this: old lines are dropped


class _Sink:
    """A `TextIO` for transcript.Writer: keeps the lines of one refresh."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> int:
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def take(self) -> list[str]:
        lines = "".join(self.parts).splitlines()
        self.parts.clear()
        return lines


class Feed:
    """The transcript of one session as a growing list of lines (only new bytes are read)."""

    def __init__(self, provider: Provider, log_path: str | Path, *, width: int = WIDTH) -> None:
        self.reader = transcript.Reader(provider, log_path)
        self.sink = _Sink()
        self.writer = transcript.Writer(self.sink, color=False, width=width)
        self.lines: list[str] = []

    def update(self) -> list[str]:
        """New lines since the previous call (capped: the tail stays, the head is dropped)."""
        self.sink.take()  # a writer that failed halfway must not be repeated
        self.writer.write(self.reader.read())
        new = self.sink.take()
        self.lines.extend(new)
        if len(self.lines) > MAX_LINES:
            self.lines = self.lines[-MAX_LINES:]
        return new

    def prompt(self) -> str:
        """The full text of the last prompt of the session (the `p` key)."""
        return self.reader.prompts[-1].text if self.reader.prompts else ""


class LiveView:
    """What the transcript screen shows for one task: the chosen session, its lines and the header.

    By default it follows the latest session of the task and takes over a newer one; `r`, `[` and `]`
    pin the human's choice (no more auto-switch). No actions, no writes — the screen is a reader.

    The width the lines are rendered for is the width of the pane: the screen tells it at mount
    (`set_width`), a narrow terminal must not get lines cut at 120 columns and re-wrapped.
    """

    def __init__(self, store: Store, task_id: int, *, width: int = WIDTH) -> None:
        self.store = store
        self.task_id = task_id
        self.width = width
        self.session: Session | None = None
        self.feed: Feed | None = None
        self.following = True  # the human scrolled up — the tail holds
        self.round = 0
        self.role = ""
        self._note: list[str] = []  # why there are no lines (no session, no log)
        self._pinned = False
        self._load(self._latest())

    def set_width(self, width: int) -> None:
        """The width of the pane the lines go into (a result line is clipped to it).

        A width that is not the one the lines were rendered for means another pane (the screen mounted,
        the terminal was resized): the history is read again, so nothing is left cut at the old width.
        """
        if width <= 0 or width == self.width:
            return
        self.width = width
        if self.session is not None:
            self._load(self.session)
        elif self.feed is not None:
            self.feed.writer.width = width

    @property
    def lines(self) -> list[str]:
        """The transcript so far: the lines of the session, or why there are none (capped)."""
        if self.feed is not None:
            return self.feed.lines[-MAX_LINES:]
        return self._note[-MAX_LINES:]

    # --- picking a session ---

    def _sessions(self) -> list[Session]:
        return self.store.list_sessions(self.task_id)

    def _latest(self) -> Session | None:
        rows = self._sessions()
        return rows[-1] if rows else None

    def _pick(self, round_no: int, role: str | None = None) -> Session | None:
        """The last session of one round, of one role (the last of the round without a role)."""
        rows = [s for s in self._sessions() if s.round == round_no]
        if role is None:
            return rows[-1] if rows else None
        return next((s for s in reversed(rows) if s.role == role), None)

    def switch_role(self) -> bool:
        """`r` — the next role of the current round (executor / reviewer)."""
        roles: list[str] = []
        for s in self._sessions():
            if s.round == self.round and s.role not in roles:
                roles.append(s.role)
        if len(roles) < 2:
            return False
        here = roles.index(self.role) if self.role in roles else -1
        return self._open(self._pick(self.round, roles[(here + 1) % len(roles)]))

    def step_round(self, delta: int) -> bool:
        """`[` / `]` — the previous / next round with sessions; the role stays if it is there."""
        rounds = sorted({s.round for s in self._sessions()})
        if not rounds or self.round not in rounds:
            return False
        target = rounds[min(max(rounds.index(self.round) + delta, 0), len(rounds) - 1)]
        if target == self.round:
            return False
        return self._open(self._pick(target, self.role) or self._pick(target))

    # --- the lines ---

    def _open(self, row: Session | None) -> bool:
        """Attach to one session and read its history; the lines start over."""
        if row is None or (self.session is not None and row.id == self.session.id
                           and row.round == self.session.round and row.role == self.session.role):
            return False
        self._load(row)
        self._pinned = True
        return True

    def _load(self, row: Session | None) -> None:
        self.session = row
        self.feed = None
        self._note = []
        if row is None:
            self._note = [_t("tui.live.no_sessions")]
            return
        self.round, self.role = row.round, row.role
        log = Path(row.log_path) if row.log_path else None
        if log is None or not log.is_file():
            self._note = [_t("tui.live.no_log", path=row.log_path or "—")]
            return
        try:
            provider = providers.get(row.provider)
        except KeyError:
            self._note = [_t("follow.unknown_provider", provider=row.provider)]
            return
        self.feed = Feed(provider, log, width=self.width)
        self.feed.update()

    def update(self) -> bool:
        """True when the lines changed — new bytes, or a newer session took over."""
        if not self._pinned:
            newest = self._latest()
            if newest is not None and (self.session is None or newest.id != self.session.id):
                self._load(newest)
                return True
        return bool(self.feed and self.feed.update())

    # --- what the header shows ---

    def header(self, pulses: dict | None = None) -> str:
        """Task, model, role, round, phase, pulse and whether the tail follows the log."""
        t = self.store.get_task(self.task_id)
        parts = [t.label if t is not None else f"T{self.task_id}"]
        if self.session is None:
            parts.append(_t("tui.live.no_session"))
        else:
            from ahub import registry as _reg

            sref = _reg.model_ref(self.session.model or "", getattr(self.session, "effort", "") or "")
            parts += [self.session.role, sref or "—",
                      f"{_t('tui.col_round')} {self.session.round}"]
        if t is not None and t.state in ACTIVE and t.phase:
            parts.append(PHASE.get(t.phase, t.phase))
        pl = (pulses or {}).get(self.task_id)
        if pl is not None:
            parts.append(f"{pl.mark} {pl.reason}".rstrip())
        parts.append(_t("tui.live.follow") if self.following else _t("tui.live.hold"))
        return " · ".join(parts)

    def prompt(self) -> str:
        """The full prompt of the last turn of the session (the `p` key)."""
        if self.feed is None:
            return _t("tui.live.no_session")
        return self.feed.prompt() or _t("tui.live.no_prompt")
