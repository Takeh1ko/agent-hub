"""Живой экран hub top: та же картина, что hub status, но таблицей."""

from __future__ import annotations

import asyncio
import subprocess
from typing import TYPE_CHECKING

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Footer, Static

from hub import time as ht
from hub.read import procs
from hub.read import snapshot
from hub.store import Store
from hub.tui.widgets import EventFeed, TaskTable

if TYPE_CHECKING:
    from hub.read.snapshot import Snapshot

try:  # H04: только импорт, без копирования логики
    from hub.read.findings import load_findings  # type: ignore
except ImportError:  # H04 ещё не применён
    load_findings = None  # type: ignore

GO_LIMIT = 60.0
FEED_TAIL = 50


class ConfirmMerge(ModalScreen[bool]):
    """Модальное подтверждение merge."""

    BINDINGS = [("y", "confirm", "да"), ("n", "cancel", "нет"), ("escape", "cancel", "нет")]

    def __init__(self, task_id: str) -> None:
        super().__init__()
        self._task_id = task_id

    def compose(self) -> ComposeResult:
        yield Static(f"Смержить {self._task_id}? (y — да, n — нет)", id="confirm-text")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HubApp(App):
    """Экран top. Источник данных — только snapshot.build каждые 2 с."""

    CSS = """
    #top-header { height: 3; }
    #tasks { height: 1fr; }
    #details { height: 9; border: solid #666; }
    #events { height: 8; }
    """

    BINDINGS = [
        ("s", "stop_task", "stop"),
        ("m", "merge_task", "merge"),
        ("f", "show_findings", "findings"),
        ("q", "quit_app", "выход"),
        ("enter", "show_details", "детали"),
    ]

    def __init__(self, store: Store | None = None,
                 opencode_db: str | None = None, proc_root: str = "/proc") -> None:
        super().__init__()
        self._store_param = store
        self._opencode_db = opencode_db
        self._proc_root = proc_root
        self.project_filter: str | None = None
        self._snap: Snapshot | None = None
        self._last_event_id: int = 0
        self._details_task: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("hub top — загрузка…", id="top-header")
        yield TaskTable(id="tasks")
        yield Static("Детали: Enter по строке таблицы", id="details")
        yield EventFeed(id="events")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._schedule_refresh)
        self._apply_narrow_mode()
        self._schedule_refresh()

    def on_resize(self, *args) -> None:
        self._apply_narrow_mode()

    # --- опрос ---

    def _schedule_refresh(self) -> None:
        self.run_worker(self._do_refresh(), exclusive=True)

    async def _do_refresh(self) -> None:
        snap = await asyncio.to_thread(self._fetch_snapshot)
        if snap is not None:
            self._apply_snapshot(snap)

    def _store(self) -> Store | None:
        if self._store_param is not None:
            return self._store_param
        try:
            return Store()
        except OSError:
            return None

    def _fetch_snapshot(self) -> Snapshot | None:
        store = self._store()
        if store is None:
            return None
        try:
            # Источник данных только snapshot.build(store, now_ms(), opencode_db, proc_root).
            return snapshot.build(store, ht.now_ms(), self._opencode_db, self._proc_root)
        except OSError:
            return None

    def _apply_snapshot(self, snap: Snapshot) -> None:
        self._snap = snap
        try:
            header = self.query_one("#top-header", Static)
            header.update(self._header_text(snap))
        except Exception:
            pass
        try:
            table = self.query_one("#tasks", TaskTable)
            table.update(self._filtered(snap))
        except Exception:
            pass
        try:
            store = self._store()
            feed = self.query_one("#events", EventFeed)
            if store is not None:
                evs = store.events_since(self._last_event_id)
                if evs:
                    self._last_event_id = max(int(e.get("id", 0) or 0) for e in evs)
                    feed.push(evs[-FEED_TAIL:])
        except Exception:
            pass
        if self._details_task is not None:
            try:
                self._render_details(self._details_task)
            except Exception:
                pass
        self._apply_narrow_mode()

    def _filtered(self, snap: Snapshot) -> Snapshot:
        if not self.project_filter:
            return snap
        needle = self.project_filter
        tasks = [t for t in snap.tasks if needle in (t.project or "")]
        return snapshot.Snapshot(
            tasks=tasks, total_go=snap.total_go,
            total_usd=snap.total_usd, now_ms=snap.now_ms,
        )

    # --- шапка ---

    def _bot_alive(self) -> bool:
        try:
            live = procs.agent_procs(self._proc_root)
        except OSError:
            return False
        for p in live:
            blob = " ".join(p.args or [])
            if "hub" in blob and "bot" in blob:
                return True
        return False

    def _header_text(self, snap: Snapshot) -> str:
        active = sum(1 for t in snap.tasks if t.stage not in ("merged", "dropped"))
        queued = sum(1 for t in snap.tasks if t.stage == "queued")
        go = snap.total_go or 0.0
        usd = snap.total_usd or 0.0
        frac = min(1.0, go / GO_LIMIT) if GO_LIMIT > 0 else 0.0
        filled = int(frac * 10)
        bar = "█" * filled + "░" * (10 - filled)
        bot = "бот жив" if self._bot_alive() else "бот нет"
        return (
            f"активно {active} · в очереди {queued} · "
            f"$ сегодня go {go:.2f} usd {usd:.2f} · "
            f"Go [{bar}] {go:.2f}/60 · agy — · {bot}"
        )

    # --- узкий режим ---

    def _apply_narrow_mode(self) -> None:
        try:
            w = self.size.width
            h = self.size.height
        except Exception:
            return
        narrow = w <= 80 or h <= 24
        for wid_id in ("#details", "#events"):
            try:
                self.query_one(wid_id).display = not narrow
            except Exception:
                continue

    # --- детали ---

    def _current_task_id(self) -> str | None:
        try:
            table = self.query_one("#tasks", TaskTable)
        except Exception:
            return None
        try:
            rows = table.ordered_rows
            idx = int(table.cursor_row)
            if 0 <= idx < len(rows):
                key = rows[idx].key
                val = getattr(key, "value", key)
                return str(val) if val else None
        except Exception:
            return None
        return None

    def _render_details(self, task_id: str) -> None:
        self._details_task = task_id
        snap = self._snap
        task = None
        if snap is not None:
            for t in snap.tasks:
                if str(t.id) == task_id:
                    task = t
                    break
        lines: list[str] = [f"Задача {task_id}"]
        if task is None:
            lines.append("(нет в снимке)")
        else:
            lines.append(f"этап {task.stage} · раунд {task.round} · пульс {task.pulse}")
            lines.append("Сессии:")
            if task.sessions:
                for s in task.sessions:
                    lines.append(
                        f" {s.role}:{s.model} {s.pulse} "
                        f"${s.cost:.3f} ctx {s.context_tokens} {s.last_activity}"[:100]
                    )
            else:
                lines.append(" —")
            lines.append("Tool-вызовы (последние 20):")
            shown = 0
            for s in task.sessions or []:
                if s.last_activity and s.last_activity != "-":
                    lines.append(f" {s.last_activity}"[:80])
                    shown += 1
                    if shown >= 20:
                        break
            if shown == 0:
                lines.append(" —")
            lines.append("Todo:")
            lines.append(" —")
            lines.append("Замечания:")
            lines.extend(self._finding_lines(task_id))
        try:
            self.query_one("#details", Static).update("\n".join(lines))
        except Exception:
            pass

    def _finding_lines(self, task_id: str) -> list[str]:
        if load_findings is None:
            return [" (findings H04 нет)"]
        store = self._store()
        wt = ""
        try:
            if store is not None:
                row = store.get_task(task_id)
                wt = str((row or {}).get("worktree") or "")
        except OSError:
            return [" (нет worktree)"]
        if not wt:
            return [" (нет worktree)"]
        try:
            from pathlib import Path
            items = load_findings(Path(wt))
        except Exception:
            return [" (не читаются)"]
        if not items:
            return [" —"]
        out = []
        for f in items[:10]:
            try:
                out.append(f"{f.file}:{f.line} [{f.severity}] {f.issue[:80]} ({f.author})")
            except Exception:
                continue
        return out or [" —"]

    # --- клавиши ---

    def action_show_details(self) -> None:
        tid = self._current_task_id()
        if tid:
            self._render_details(tid)
            try:
                self.query_one("#details", Static).display = True
            except Exception:
                pass

    def on_data_table_row_selected(self, event) -> None:  # type: ignore[no-untyped-def]
        try:
            tid = str(event.row_key.value or "")
        except Exception:
            return
        if tid:
            self._render_details(tid)

    def action_quit_app(self) -> None:
        self.exit()

    def action_stop_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("нет задачи", severity="warning")
            return
        self.run_worker(self._run_hub_cmd(["stop", tid]), exclusive=True)

    def action_merge_task(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("нет задачи", severity="warning")
            return

        def _done(ok: bool | None) -> None:
            if ok:
                self.run_worker(self._run_hub_cmd(["merge", tid]), exclusive=True)

        self.push_screen(ConfirmMerge(tid), _done)

    def action_show_findings(self) -> None:
        tid = self._current_task_id()
        if not tid:
            self.notify("нет задачи", severity="warning")
            return
        if load_findings is not None:
            self._render_details(tid)
            return
        self.run_worker(self._run_hub_cmd(["findings", tid]), exclusive=True)

    async def _run_hub_cmd(self, argv: list[str]) -> None:
        cmd = ["hub", *argv]
        try:
            proc = await asyncio.to_thread(
                subprocess.run, cmd, capture_output=True, text=True, timeout=60,
            )
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()
            self.notify(tail[-1][:80] if tail else "готово")
        except Exception as e:
            self.notify(f"не вышло: {e}"[:80], severity="error")
        self._schedule_refresh()


if __name__ == "__main__":  # pragma: no cover
    HubApp().run()
