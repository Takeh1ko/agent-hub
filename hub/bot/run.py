"""Тонкая обвязка aiogram: сеть здесь, логика — в hub.bot.core.

Блокирующий код (sqlite, snapshot) — только в sync-хелперах _sync_*,
из event loop они зовутся через asyncio.to_thread. Шаги циклов
(outbox_once/poll_once) — без sleep, тестируются с фейковым ботом.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from hub.bot import core as bc

OUTBOX_S = 5
POLL_S = 15
ROSTER_S = 60 * 60

# bot_msg_id → question_id для ответов реплаем (только event loop трогает).
_QMSG: dict[tuple[int, int], int] = {}
# question_id → все копии вопроса по чатам (для правки веером).
_QSENT: dict[int, list[tuple[int, int]]] = {}
_QMSG_MAX = 1000

# path → настроен ли хендлер (изоляция тестов с разным HOME).
_log_ready: set[str] = set()


def _log_path() -> Path:
    """Путь лога, HOME читается при вызове (тесты подменяют HOME)."""
    return Path.home() / ".local/state/agent-hub/bot.log"


def setup_logging() -> logging.Logger:
    """Лог в ~/.local/state/agent-hub/bot.log, ротация 5×1 МБ."""
    path = _log_path()
    key = str(path)
    logger = logging.getLogger("hub.bot")
    logger.setLevel(logging.INFO)
    if key not in _log_ready:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            key, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        _log_ready.add(key)
    return logger


def get_proxy() -> str | None:
    """Прокси из HTTPS_PROXY (на ПК Telegram только через VPN)."""
    return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


class HttpProxySession(AiohttpSession):
    """Aiohttp-сессия с HTTP-прокси без пакета aiohttp-socks.

    aiogram 3.31 при любом proxy= в конструкторе строит ProxyConnector,
    которому нужен aiohttp-socks (нет в зависимостях — старт падал
    с RuntimeError до цикла с повтором). Нативный aiohttp умеет
    HTTP-прокси через параметр proxy= самого запроса — для HTTPS_PROXY
    вида http://host:port этого достаточно.
    """

    def __init__(self, proxy_url: str, **kwargs) -> None:
        super().__init__(proxy=None, **kwargs)
        self._proxy_url = str(proxy_url)

    @property
    def proxy_url(self) -> str:
        """Куда идут запросы."""
        return self._proxy_url

    async def make_request(self, bot, method, timeout=None):
        """Как базовая, но proxy= уходит в session.post."""
        from aiogram.exceptions import TelegramNetworkError

        session = await self.create_session()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        form = self.build_form_data(bot=bot, method=method)
        try:
            async with session.post(
                url,
                data=form,
                proxy=self._proxy_url,
                timeout=self.timeout if timeout is None else timeout,
            ) as resp:
                raw_result = await resp.text()
        except asyncio.TimeoutError as e:
            raise TelegramNetworkError(
                method=method, message="Request timeout error") from e
        except Exception as e:  # noqa: BLE001 — как базовая ClientError-ветка
            from aiohttp import ClientError

            if not isinstance(e, ClientError):
                raise
            raise TelegramNetworkError(
                method=method, message=f"{type(e).__name__}: {e}") from e
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=resp.status,
            content=raw_result,
        )
        return response.result

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=65536, raise_for_status=True):
        """Как базовая, но proxy= уходит в session.get."""
        if headers is None:
            headers = {}
        session = await self.create_session()
        async with session.get(
            url,
            proxy=self._proxy_url,
            timeout=timeout,
            headers=headers,
            raise_for_status=raise_for_status,
        ) as resp:
            async for chunk in resp.content.iter_chunked(chunk_size):
                yield chunk


def build_session():
    """Сессия aiogram: с прокси из HTTPS_PROXY либо None (напрямую).

    Не бросает исключений отсутствия aiohttp-socks: HTTP-прокси идёт
    через нативный параметр запроса aiohttp.
    """
    proxy = get_proxy()
    if not proxy:
        return None
    return HttpProxySession(proxy)


def _markup(buttons: list[bc.Button]):
    """Кнопки core → InlineKeyboardMarkup (чисто, без сети/БД)."""
    if not buttons:
        return None
    from aiogram.types import InlineKeyboardButton

    rows = [[InlineKeyboardButton(text=b.label, callback_data=b.data)]
            for b in buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _no_buttons():
    """Явно снять кнопки: aiogram не кладёт reply_markup=None в запрос,
    а без поля Telegram оставляет прежнюю клавиатуру."""
    return InlineKeyboardMarkup(inline_keyboard=[])


def _remember_qmsg(chat_id: int, bot_msg_id: int, qid: int) -> None:
    """Запомнить соответствие, с капой от утечки.

    _QMSG (резолв реплаев) и _QSENT (веерная правка) чистятся синхронно:
    вытеснение из одной структуры убирает ключ и из другой.
    """
    key = (int(chat_id), int(bot_msg_id))
    qid = int(qid)
    old = _QMSG.get(key)
    if old is not None and old != qid:
        _drop_qmsg_key(old, key)
    _QMSG[key] = qid
    sent = _QSENT.setdefault(qid, [])
    if key not in sent:
        sent.append(key)
    while len(_QMSG) > _QMSG_MAX:
        oldest = next(iter(_QMSG))
        _drop_qmsg_key(_QMSG.pop(oldest), oldest)
    while len(_QSENT) > _QMSG_MAX:
        _drop_qsent_qid(next(iter(_QSENT)))


def _drop_qmsg_key(qid: int, key: tuple[int, int]) -> None:
    """Убрать (chat, msg) из веерного списка вопроса."""
    sent = _QSENT.get(int(qid))
    if sent is None:
        return
    try:
        sent.remove(key)
    except ValueError:
        pass
    if not sent:
        _QSENT.pop(int(qid), None)


def _drop_qsent_qid(qid: int) -> None:
    """Вытеснить вопрос из _QSENT вместе с ключами _QMSG."""
    for key in _QSENT.pop(int(qid), []):
        if _QMSG.get(key) == int(qid):
            _QMSG.pop(key, None)


def _forget_qid(qid: int) -> None:
    """Убрать отвеченный вопрос из mapping реплаев.

    _QSENT оставляем: опоздавший тап «Уже отвечен» тоже должен
    гасить клавиатуру в копиях других чатов.
    """
    for key in [k for k, v in _QMSG.items() if v == int(qid)]:
        _QMSG.pop(key, None)


def _resolve_qid(chat_id: int, replied_msg_id: int,
                 replied_text: str) -> int | None:
    """qid по mapping, иначе fallback — парсинг «Вопрос #id» из текста."""
    hit = _QMSG.get((int(chat_id), int(replied_msg_id)))
    if hit is not None:
        return hit
    return bc.parse_question_ref(replied_text or "")


def _qmsg_keys(qid: int) -> list[tuple[int, int]]:
    """Все (chat_id, message_id), куда ушёл вопрос (все чаты рассылки)."""
    return list(_QSENT.get(int(qid), []))


@dataclass
class BotState:
    """Память циклов: дедуп, diff-база, per-chat доставки."""

    grouper: bc.Grouper = field(default_factory=bc.Grouper)
    prev_snap: object = None
    last_roster_norm: str | None = None
    last_roster_ts: int = 0
    outbox_done: set[tuple[int, int]] = field(default_factory=set)
    q_done: set[tuple[int, int]] = field(default_factory=set)
    summary_done: set[tuple[int, int]] = field(default_factory=set)
    # Замороженный батч сводки (текст + id) до полной доставки всем чатам:
    # пересчёт из всего буфера давал бы дубли получившим при новом событии.
    sum_text: str | None = None
    sum_batch: list[int] = field(default_factory=list)
    # Отложенные пульс-сигналы snapshot_events (гистерезис, см. core).
    pulse_pending: dict = field(default_factory=dict)
    # Per-chat доставка авторостера: (norm, chat).
    roster_done: set[tuple[str, int]] = field(default_factory=set)


# --- sync-хелперы: вызываются только через asyncio.to_thread ---

def _sync_remember(chat_id: int, now_ms: int) -> None:
    from hub.store import Store

    bc.remember_chat(Store(), int(chat_id), int(now_ms))


def _sync_owner_text(chat_id: int, text: str, now_ms: int) -> str:
    from hub.store import Store

    return bc.handle_owner_text(Store(), int(chat_id), str(text), int(now_ms))


def _sync_all_chats() -> list[int]:
    from hub.store import Store

    return bc.all_chats(Store())


def _sync_outbox_rows() -> list[dict]:
    from hub.store import Store

    return bc.fetch_outbox(Store())


def _sync_outbox_mark(oid: int, now_ms: int) -> None:
    from hub.store import Store

    bc.mark_outbox_sent(Store(), int(oid), int(now_ms))


def _opencode_db() -> str | None:
    db = Path.home() / ".local/share/opencode/opencode.db"
    return str(db) if db.exists() else None


def _sync_status(now_ms: int) -> str:
    """Статус как `hub status` без --all: без merged/dropped."""
    from hub.commands.status import auto_import
    from hub.read import snapshot as snap
    from hub.store import FINAL_STAGES, Store

    store = Store()
    try:
        auto_import(store)
    except Exception:
        pass
    s = snap.build(store, int(now_ms),
                   opencode_db=_opencode_db(), proc_root="/proc")
    s.tasks = [t for t in s.tasks if t.stage not in FINAL_STAGES]
    return bc.format_status(s)


def _sync_roster(now_ms: int) -> str:
    from hub.commands.status import auto_import
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    try:
        auto_import(store)
    except Exception:
        pass
    s = snap.build(store, int(now_ms),
                   opencode_db=_opencode_db(), proc_root="/proc")
    return bc.format_roster(s)


def _sync_task(task_id: str, now_ms: int) -> str:
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    s = snap.build(store, int(now_ms),
                   opencode_db=_opencode_db(), proc_root="/proc")
    return bc.format_task(store, str(task_id), snapshot=s)


def _sync_budget() -> str:
    from hub.store import Store

    return bc.format_budget(Store())


def _sync_pause(paused: bool) -> str:
    from hub.store import Store

    return bc.set_paused(Store(), bool(paused))


def _sync_confirm(action: str, task_id: str, ok: bool, now_ms: int) -> str:
    from hub.store import Store

    return bc.apply_confirm(Store(), str(action), str(task_id),
                            bool(ok), int(now_ms))


def _sync_answer(qid: int, text: str, now_ms: int) -> tuple[bool, str, str]:
    """(ок, короткий ответ, полный текст для правки вопроса)."""
    from hub.store import Store

    store = Store()
    reason = bc.answer_question_reason(store, int(qid), str(text),
                                       via="tg", now_ms=int(now_ms))
    if reason == bc.ANSWER_NOT_FOUND:
        return False, "Вопрос не найден.", ""
    if reason == bc.ANSWER_EMPTY:
        return False, "Пустой ответ — нечего записывать.", ""
    if reason != bc.ANSWER_OK:
        return False, "Уже отвечен.", ""
    row = bc.get_question(store, int(qid))
    full = bc.format_answered_text(row, str(text)) if row else "✅ Ответ записан"
    return True, "✅ Ответ записан", full


def _sync_answer_cb(data: str, now_ms: int) -> tuple[bool, str]:
    from hub.store import Store

    return bc.answer_by_callback(Store(), str(data), via="tg",
                                 now_ms=int(now_ms))


def _sync_events_take() -> list[dict]:
    from hub.store import Store

    return bc.fetch_unsent_notifiable(Store())


def _sync_events_mark(ids: list[int]) -> None:
    from hub.store import Store

    bc.mark_events_sent(Store(), [int(i) for i in ids])


def _sync_new_questions() -> list[dict]:
    from hub.store import Store

    return bc.new_questions_to_send(Store())


def _sync_q_mark(qid: int) -> None:
    from hub.store import Store

    bc.mark_question_sent(Store(), int(qid))


def _sync_forget(chat_id: int) -> None:
    """Удалить мёртвый чат из tg_chat (Telegram: not found/blocked)."""
    from hub.store import Store

    bc.forget_chat(Store(), int(chat_id))


# --- H15 /new: черновики в фоне, бот не блокируется ---

# chat_id → {"stage": "project"|"text", "project": str}: ждём выбора/текста.
_NEW_PENDING: dict[int, dict] = {}
# (chat_id, bot_msg_id) → draft_id: предпросмотр для «Изменить» ответом.
_DMSG: dict[tuple[int, int], int] = {}
_DMSG_MAX = 1000


def _remember_dmsg(chat_id: int, bot_msg_id: int, draft_id: int) -> None:
    key = (int(chat_id), int(bot_msg_id))
    _DMSG[key] = int(draft_id)
    while len(_DMSG) > _DMSG_MAX:
        _DMSG.pop(next(iter(_DMSG)))


def _resolve_draft(chat_id: int, replied_msg_id: int) -> int | None:
    return _DMSG.get((int(chat_id), int(replied_msg_id)))


def _sync_projects() -> list:
    from hub.config import load_projects

    try:
        return load_projects()
    except (OSError, ValueError):
        return []


def _sync_make_draft(project_name: str, text: str, chat_id: int,
                     now_ms: int) -> int:
    """Создать черновик моделью (блокирует — звать через to_thread)."""
    from hub.config import load_projects
    from hub.pipeline.draft import make_draft, make_runner_for_project
    from hub.store import Store

    store = Store()
    project = None
    for p in load_projects():
        if str(getattr(p, "name", "") or "") == str(project_name):
            project = p
            break
    if project is None:
        raise ValueError(f"нет проекта {project_name}")
    runner = make_runner_for_project(project)
    return int(make_draft(store, project, str(text), "tg", runner,
                          chat_id=int(chat_id), now_ms=int(now_ms)))


def _sync_draft_preview(draft_id: int) -> tuple[str, list]:
    """Текст предпросмотра + кнопки. Пусто — черновик не ready."""
    from hub.store import Store

    store = Store()
    row = store.get_draft(int(draft_id))
    if row is None:
        return "Черновик не найден.", []
    if str(row.get("status") or "") != "ready":
        errs = str(row.get("lint_errors") or "").strip()
        head = f"Черновик {draft_id} не готов ({row.get('status')})."
        return (head + ("\n" + errs[:1000] if errs else "")), []
    text = bc.format_draft_preview(row, str(row.get("card_text") or ""))
    return text, bc.draft_buttons(int(draft_id))


def _sync_start_draft(draft_id: int, now_ms: int) -> str:
    """Запустить черновик: задача в очереди. Возвращает task_id."""
    from hub.pipeline.draft import start_draft
    from hub.store import Store

    return str(start_draft(Store(), int(draft_id), now_ms=int(now_ms)))


def _sync_cancel_draft(draft_id: int) -> None:
    from hub.pipeline.draft import cancel_draft
    from hub.store import Store

    cancel_draft(Store(), int(draft_id))


async def _draft_bg(bot, chat_id: int, project_name: str, text: str,
                    now_ms: int) -> None:
    """Фоновая готовка черновика: «Пишу карточку…» → предпросмотр с кнопками."""
    log = logging.getLogger("hub.bot")
    try:
        wait_msg = await bot.send_message(int(chat_id), bc.NEW_WRITING)
        wait_id = int(getattr(wait_msg, "message_id", 0) or 0)
    except Exception as e:  # noqa: BLE001 — не отправилось «Пишу…», дальше всё равно
        log.warning("draft wait → %s: %s", chat_id, e)
        wait_id = 0
    try:
        draft_id = await asyncio.to_thread(
            _sync_make_draft, str(project_name), str(text),
            int(chat_id), int(now_ms))
    except Exception as e:  # noqa: BLE001 — модель/проект упал, владелец видит причину
        try:
            await bot.send_message(int(chat_id), f"Не вышло: {e}"[:500])
        except Exception:
            pass
        return
    preview, buttons = await asyncio.to_thread(_sync_draft_preview, draft_id)
    try:
        sent = await bot.send_message(
            int(chat_id), bc.clip(preview),
            reply_markup=_markup(buttons) if buttons else None)
        _remember_dmsg(int(chat_id), int(sent.message_id), int(draft_id))
    except Exception as e:  # noqa: BLE001 — best-effort
        log.warning("draft preview → %s: %s", chat_id, e)
        return
    if wait_id:
        try:
            await bot.edit_message_text(
                text=bc.NEW_WRITING + f" готово: черновик {draft_id}",
                chat_id=int(chat_id), message_id=wait_id)
        except Exception:  # noqa: BLE001 — best-effort
            pass


async def handle_draft_callback(bot, chat_id: int, data: str,
                                now_ms: int) -> str | None:
    """Тап по кнопкам черновика: проект → просим текст; старт/отмена → применяем.

    None — кнопку не понял. Тестируется без сети (бот — фейк).
    """
    await asyncio.to_thread(_sync_remember, int(chat_id), int(now_ms))
    parsed = bc.parse_draft_callback(str(data or ""))
    if parsed is None:
        return None
    action, target = parsed
    if action == "project":
        projects = await asyncio.to_thread(_sync_projects)
        names = {str(getattr(p, "name", "") or "") for p in projects}
        if target not in names:
            return bc.NEW_UNKNOWN_PROJECT
        _NEW_PENDING[int(chat_id)] = {"stage": "text", "project": target}
        return bc.NEW_ASK_TEXT
    if action == "cancel":
        try:
            await asyncio.to_thread(_sync_cancel_draft, int(target))
        except (ValueError, OSError) as e:
            return f"Не отменился: {e}"[:300]
        _NEW_PENDING.pop(int(chat_id), None)
        return f"Черновик {target} отменён."
    # action == "start": запуск без ожидания модели — быстро, в этом же апдейте.
    try:
        task_id = await asyncio.to_thread(
            _sync_start_draft, int(target), int(now_ms))
    except (ValueError, OSError) as e:
        return f"Не запустился: {e}"[:500]
    _NEW_PENDING.pop(int(chat_id), None)
    return f"OK {task_id}: задача в очереди, Claude уведомлён."


def _sync_tick(prev_snap, now_ms: int, pending: dict | None = None):
    """Продюсер сводок: snapshot.build → дифф → события в store.

    Возвращает (cur_snap, добавленные_строки_с_id). Вызывается в потоке.
    pending — отложенные пульс-сигналы (гистерезис, живёт в BotState);
    None — немедленные пульс-события (старое поведение).
    """
    from hub.read import snapshot as snap
    from hub.store import Store

    store = Store()
    cur = snap.build(store, int(now_ms),
                     opencode_db=_opencode_db(), proc_root="/proc")
    produced = bc.snapshot_events(prev_snap, cur, pending)
    produced += bc.pop_budget_events(store, cur)
    added = bc.record_events(store, produced, int(now_ms)) if produced else []
    return cur, added


async def _edit_question_everywhere(bot, qid: int, text: str) -> int:
    """Править копии вопроса во всех чатах рассылки. Возвращает число правок.

    Карточка п.4: у отвеченного вопроса кнопки убираются — передаём явно
    пустую клавиатуру (без поля Telegram оставил бы прежние кнопки).
    """
    log = logging.getLogger("hub.bot")
    n = 0
    for c, m in _qmsg_keys(int(qid)):
        try:
            await bot.edit_message_text(
                text=bc.clip(text), chat_id=int(c), message_id=int(m),
                reply_markup=_no_buttons())
            n += 1
        except Exception as e:  # noqa: BLE001 — best-effort правка
            log.debug("правка вопроса %s в %s: %s", qid, c, e)
            continue
    return n


async def _drop_question_keyboards(bot, qid: int) -> int:
    """Убрать клавиатуру у копий вопроса во всех чатах (уже отвечен)."""
    n = 0
    for c, m in _qmsg_keys(int(qid)):
        try:
            await bot.edit_message_reply_markup(
                chat_id=int(c), message_id=int(m),
                reply_markup=_no_buttons())
            n += 1
        except Exception:  # noqa: BLE001 — best-effort
            continue
    return n


async def handle_qans(bot, chat_id: int, data: str, now_ms: int) -> tuple[bool, str]:
    """Тап по варианту ответа: remember чата, запись, правка всех копий.

    Тестируется с фейковым ботом (без сети).
    """
    await asyncio.to_thread(_sync_remember, int(chat_id), int(now_ms))
    ok, text = await asyncio.to_thread(
        _sync_answer_cb, str(data), int(now_ms))
    parsed = bc.parse_answer_callback(str(data or ""))
    if parsed is not None:
        if ok:
            await _edit_question_everywhere(bot, parsed[0], text)
            _forget_qid(parsed[0])
        elif text == "Уже отвечен.":
            await _drop_question_keyboards(bot, parsed[0])
    return ok, text


async def handle_confirm(chat_id: int, data: str, now_ms: int) -> str | None:
    """Подтверждение stop/merge: remember чата, применение.

    None — кнопку не понял. Тестируется без сети.
    """
    await asyncio.to_thread(_sync_remember, int(chat_id), int(now_ms))
    parsed = bc.parse_confirm(str(data or ""))
    if parsed is None:
        return None
    action, tid, ok = parsed
    return await asyncio.to_thread(
        _sync_confirm, action, tid, ok, int(now_ms))


async def handle_reply(bot, chat_id: int, qid: int, body: str,
                       replied_id: int, now_ms: int) -> tuple[bool, str]:
    """Ответ реплаем: remember чата, запись + правка копий во всех чатах."""
    await asyncio.to_thread(_sync_remember, int(chat_id), int(now_ms))
    ok, short, full = await asyncio.to_thread(
        _sync_answer, int(qid), str(body), int(now_ms))
    if not ok:
        return False, short
    targets = set(_qmsg_keys(int(qid)))
    if replied_id:
        targets.add((int(chat_id), int(replied_id)))
    for c, m in targets:
        try:
            await bot.edit_message_text(
                text=bc.clip(full), chat_id=int(c), message_id=int(m),
                reply_markup=_no_buttons())
        except Exception:  # noqa: BLE001 — best-effort правка
            continue
    _forget_qid(int(qid))
    return True, short


async def show_confirm_result(message, text: str) -> None:
    """Заменить текст подтверждения и снять кнопки Да/Нет (шаг израсходован).

    Правка падает (сообщение удалили) — отвечаем новым сообщением.
    """
    body = bc.clip(text)
    try:
        await message.edit_text(body, reply_markup=_no_buttons())
    except Exception:  # noqa: BLE001 — сообщение могли удалить
        await message.answer(body)


# --- шаги циклов без sleep (тестируются с фейковым ботом) ---

async def outbox_once(bot, state: BotState, now_ms: int) -> int:
    """Одна итерация outbox: каждому чату независимо, без дублей.

    Мёртвый чат (chat not found / bot was blocked / Forbidden) удаляется
    из tg_chat и не держит доставку остальным.
    Возвращает число строк, полностью доставленных на этом проходе.
    """
    log = logging.getLogger("hub.bot")
    rows = await asyncio.to_thread(_sync_outbox_rows)
    if not rows:
        return 0
    chats = await asyncio.to_thread(_sync_all_chats)
    done_rows = 0
    for row in rows:
        oid = int(row["id"])
        text = bc.clip(str(row.get("text") or ""))
        if not text.strip():
            await asyncio.to_thread(_sync_outbox_mark, oid, now_ms)
            state.outbox_done = {(o, c) for o, c in state.outbox_done if o != oid}
            done_rows += 1
            continue
        for chat in chats:
            if (oid, int(chat)) in state.outbox_done:
                continue
            try:
                await bot.send_message(int(chat), text)
            except Exception as e:  # noqa: BLE001 — ретрай только этому чату
                if bc.is_dead_chat_error(e):
                    log.warning("мёртвый чат %s, удаляю: %s", chat, e)
                    await asyncio.to_thread(_sync_forget, int(chat))
                    state.outbox_done.add((oid, int(chat)))
                else:
                    log.warning("outbox %s → %s: %s", oid, chat, e)
                continue
            state.outbox_done.add((oid, int(chat)))
        if all((oid, int(c)) in state.outbox_done for c in chats):
            await asyncio.to_thread(_sync_outbox_mark, oid, now_ms)
            state.outbox_done = {(o, c) for o, c in state.outbox_done if o != oid}
            done_rows += 1
    return done_rows


async def poll_once(bot, state: BotState, now_ms: int) -> dict:
    """Одна итерация 15 с: вопросы, продюсер, группировка, авторостер."""
    log = logging.getLogger("hub.bot")
    res = {"questions": 0, "added": 0, "sent": 0, "roster": False}
    # 1. Продюсер: снимок → разница → события (пульс — с гистерезисом).
    cur, added = await asyncio.to_thread(
        _sync_tick, state.prev_snap, now_ms, state.pulse_pending)
    state.prev_snap = cur
    res["added"] = len(added)
    # 2. Вопросы новым чатам — per-chat, без дублей получившим.
    for q in await asyncio.to_thread(_sync_new_questions):
        qid = int(q["id"])
        text, buttons = bc.format_question(q)
        chats = await asyncio.to_thread(_sync_all_chats)
        markup = _markup(buttons)
        for chat in chats:
            if (qid, int(chat)) in state.q_done:
                continue
            try:
                sent = await bot.send_message(
                    int(chat), bc.clip(text), reply_markup=markup)
                _remember_qmsg(int(chat), int(sent.message_id), qid)
            except Exception as e:  # noqa: BLE001 — ретрай этому чату
                if bc.is_dead_chat_error(e):
                    log.warning("мёртвый чат %s, удаляю: %s", chat, e)
                    await asyncio.to_thread(_sync_forget, int(chat))
                    state.q_done.add((qid, int(chat)))
                else:
                    log.warning("вопрос %s → %s: %s", qid, chat, e)
                continue
            state.q_done.add((qid, int(chat)))
        if all((qid, int(c)) in state.q_done
               for c in await asyncio.to_thread(_sync_all_chats)):
            await asyncio.to_thread(_sync_q_mark, qid)
            state.q_done = {(q_, c) for q_, c in state.q_done if q_ != qid}
            res["questions"] += 1
    # 3. События в группировку (дедуп seen), flush только после успеха.
    for ev in added:
        state.grouper.add(ev, now_ms)
    for ev in await asyncio.to_thread(_sync_events_take):
        state.grouper.add(ev, now_ms)
    if state.grouper.buf and state.grouper.ready(now_ms):
        chats = await asyncio.to_thread(_sync_all_chats)
        # Текст — из целых строк (остаток — следующим сообщением),
        # доставка — per-chat: sent_tg и буфер — только когда ушло всем.
        # Батч заморожен до полной доставки: пересчёт из всего буфера
        # при новом событии давал бы дубли уже получившим чатам.
        if not state.sum_batch:
            state.sum_text, state.sum_batch = \
                bc.select_summary_batch(state.grouper.buf)
        text, batch = state.sum_text or "", list(state.sum_batch)
        if chats and text.strip() and batch:
            for chat in chats:
                if all((eid, int(chat)) in state.summary_done
                       for eid in batch):
                    continue
                try:
                    await bot.send_message(int(chat), bc.clip(text))
                except Exception as e:  # noqa: BLE001 — ретрай этому чату
                    if bc.is_dead_chat_error(e):
                        log.warning("мёртвый чат %s, удаляю: %s", chat, e)
                        await asyncio.to_thread(_sync_forget, int(chat))
                        for eid in batch:
                            state.summary_done.add((int(eid), int(chat)))
                    else:
                        log.warning("сводка → %s: %s", chat, e)
                    continue
                for eid in batch:
                    state.summary_done.add((int(eid), int(chat)))
            if all((eid, int(c)) in state.summary_done
                   for eid in batch for c in chats):
                await asyncio.to_thread(_sync_events_mark, batch)
                state.grouper.remove_ids(batch)
                state.sum_text, state.sum_batch = None, []
                if state.grouper.buf:
                    # Остаток — на следующее окно.
                    state.grouper.first_ts = int(now_ms)
                done = set(int(e) for e in batch)
                state.summary_done = {(e, c) for e, c in state.summary_done
                                      if e not in done}
                res["sent"] = len(batch)
    # 4. Авторостер раз в час, если нормализованный снимок менялся.
    # Доставка per-chat (как сводка): метка — только когда ушло всем.
    norm = bc.snapshot_key(cur)
    if state.last_roster_norm is None:
        state.last_roster_norm = norm
        state.last_roster_ts = now_ms
    elif norm != state.last_roster_norm:
        # Новая картина вытесняет недодоставленную старую.
        state.roster_done = {(n, c) for n, c in state.roster_done
                             if n == norm}
    if (state.last_roster_norm is not None
            and now_ms - state.last_roster_ts >= ROSTER_S * 1000
            and norm != state.last_roster_norm):
        roster = bc.format_roster(cur)
        chats = await asyncio.to_thread(_sync_all_chats)
        for chat in chats:
            if (norm, int(chat)) in state.roster_done:
                continue
            try:
                await bot.send_message(int(chat), roster, parse_mode="HTML")
            except Exception as e:  # noqa: BLE001 — ретрай этому чату
                if bc.is_dead_chat_error(e):
                    log.warning("мёртвый чат %s, удаляю: %s", chat, e)
                    await asyncio.to_thread(_sync_forget, int(chat))
                    state.roster_done.add((norm, int(chat)))
                else:
                    log.warning("авторостер → %s: %s", chat, e)
                continue
            state.roster_done.add((norm, int(chat)))
        if chats and all((norm, int(c)) in state.roster_done for c in chats):
            state.roster_done = {(n, c) for n, c in state.roster_done
                                 if n != norm}
            state.last_roster_norm = norm
            state.last_roster_ts = now_ms
            res["roster"] = True
    return res


# --- обвязка ---

def build_dispatcher() -> Dispatcher:
    """Собрать Dispatcher без сети и файлов (для --dry-run и тестов)."""
    router = Router()

    @router.message(Command("start"))
    async def _start(msg: Message) -> None:
        from hub import time as ht

        chat = int(msg.chat.id)
        await asyncio.to_thread(_sync_remember, chat, ht.now_ms())
        await msg.answer(bc.START_TEXT)

    @router.message(Command("help"))
    async def _help(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        await msg.answer(bc.HELP_TEXT)

    @router.message(Command("status"))
    async def _status(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_status, ht.now_ms())
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("roster"))
    async def _roster(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_roster, ht.now_ms())
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("task"))
    async def _task(msg: Message) -> None:
        from hub import time as ht

        now = ht.now_ms()
        await asyncio.to_thread(_sync_remember, int(msg.chat.id), now)
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /task ID")
            return
        tid = arg.split()[0]
        text = await asyncio.to_thread(_sync_task, tid, now)
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("stop"))
    async def _stop(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /stop ID")
            return
        tid = arg.split()[0]
        await msg.answer(bc.confirm_text("stop", tid),
                         reply_markup=_markup(bc.confirm_buttons("stop", tid)))

    @router.message(Command("merge"))
    async def _merge(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        _, arg = bc.split_command(msg.text or "")
        if not arg:
            await msg.answer("Нужен ID: /merge ID")
            return
        tid = arg.split()[0]
        await msg.answer(bc.confirm_text("merge", tid),
                         reply_markup=_markup(bc.confirm_buttons("merge", tid)))

    @router.message(Command("budget"))
    async def _budget(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_budget)
        await msg.answer(text, parse_mode="HTML")

    @router.message(Command("pause"))
    async def _pause(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_pause, True)
        await msg.answer(text)

    @router.message(Command("resume"))
    async def _resume(msg: Message) -> None:
        from hub import time as ht

        await asyncio.to_thread(
            _sync_remember, int(msg.chat.id), ht.now_ms())
        text = await asyncio.to_thread(_sync_pause, False)
        await msg.answer(text)

    @router.message(Command("new"))
    async def _new(msg: Message) -> None:
        from hub import time as ht

        chat = int(msg.chat.id)
        now = ht.now_ms()
        await asyncio.to_thread(_sync_remember, chat, now)
        _, arg = bc.split_command(msg.text or "")
        projects = await asyncio.to_thread(_sync_projects)
        if not projects:
            await msg.answer(bc.NEW_NO_PROJECTS)
            return
        want, rest = bc.parse_new_args(arg, projects)
        if want is not None and rest:
            # /new проект текст — сразу без вопросов, черновик в фоне.
            asyncio.create_task(_draft_bg(msg.bot, chat, want, rest, now))
            return
        if want is not None:
            _NEW_PENDING[chat] = {"stage": "text", "project": want}
            await msg.answer(bc.NEW_ASK_TEXT)
            return
        if rest and len(projects) == 1:
            only = str(getattr(projects[0], "name", "") or "")
            asyncio.create_task(_draft_bg(msg.bot, chat, only, rest, now))
            return
        if rest:
            # Текст есть, проекта нет — выбрать проект кнопкой, текст запомнить.
            _NEW_PENDING[chat] = {"stage": "project", "text": rest}
            await msg.answer(bc.NEW_ASK_PROJECT,
                             reply_markup=_markup(bc.project_buttons(projects)))
            return
        _NEW_PENDING[chat] = {"stage": "project"}
        await msg.answer(bc.NEW_ASK_PROJECT,
                         reply_markup=_markup(bc.project_buttons(projects)))

    @router.callback_query(F.data.startswith("qans:"))
    async def _qans(call: CallbackQuery) -> None:
        from hub import time as ht

        try:
            chat = int(call.message.chat.id)
        except (AttributeError, TypeError, ValueError):
            chat = int(call.from_user.id)
        # Правку копий делает handle_qans веером (включая тапнутую):
        # повторный edit_text давал бы вечный «message is not modified».
        ok, text = await handle_qans(
            call.bot, chat, str(call.data or ""), ht.now_ms())
        await call.answer("✅ Записано" if ok else text)

    @router.callback_query(F.data.startswith("confirm:"))
    async def _confirm(call: CallbackQuery) -> None:
        from hub import time as ht

        try:
            chat = int(call.message.chat.id)
        except (AttributeError, TypeError, ValueError):
            chat = int(call.from_user.id)
        text = await handle_confirm(chat, str(call.data or ""), ht.now_ms())
        if text is None:
            await call.answer("Не понял кнопку.")
            return
        await show_confirm_result(call.message, text)
        await call.answer("Готово")

    @router.callback_query(F.data.startswith("draft:"))
    async def _draft(call: CallbackQuery) -> None:
        from hub import time as ht

        try:
            chat = int(call.message.chat.id)
        except (AttributeError, TypeError, ValueError):
            chat = int(call.from_user.id)
        now = ht.now_ms()
        parsed = bc.parse_draft_callback(str(call.data or ""))
        if parsed is None:
            await call.answer("Не понял кнопку.")
            return
        action, target = parsed
        if action == "project":
            # Проект выбран кнопкой: был ли текст заранее — сразу в фон.
            pend = _NEW_PENDING.get(chat, {})
            saved = str(pend.get("text", "") or "").strip()
            _NEW_PENDING[chat] = {"stage": "text", "project": target}
            if saved:
                _NEW_PENDING.pop(chat, None)
                await call.answer("Пишу карточку…")
                asyncio.create_task(_draft_bg(call.bot, chat, target, saved, now))
                return
            text = await handle_draft_callback(call.bot, chat,
                                               str(call.data or ""), now)
            await call.answer("Проект выбран")
            try:
                await call.message.answer(bc.clip(text or ""))
            except Exception:  # noqa: BLE001 — best-effort
                pass
            return
        text = await handle_draft_callback(call.bot, chat,
                                           str(call.data or ""), now)
        await show_confirm_result(call.message, text or "")
        await call.answer("Готово")

    @router.message(F.text)
    async def _text(msg: Message) -> None:
        from hub import time as ht

        body = str(msg.text or "")
        chat = int(msg.chat.id)
        now = ht.now_ms()
        if body.startswith("/"):
            await asyncio.to_thread(_sync_remember, chat, now)
            await msg.answer("Не знаю команды. /help — список.")
            return
        # Ответ реплаем на вопрос бота (mapping или «Вопрос #id» в тексте).
        qid: int | None = None
        replied = getattr(msg, "reply_to_message", None)
        replied_id = 0
        replied_text = ""
        if replied is not None:
            replied_id = int(getattr(replied, "message_id", 0) or 0)
            replied_text = str(getattr(replied, "text", "") or "")
            qid = _resolve_qid(chat, replied_id, replied_text)
        if qid is not None:
            ok, short = await handle_reply(
                msg.bot, chat, qid, body, replied_id, now)
            await msg.answer(bc.clip(short))
            return
        # H15: «Изменить» — ответ на предпросмотр: текст правки → новый черновик.
        if replied_id:
            draft_id = _resolve_draft(chat, replied_id)
            if draft_id is not None:
                from hub.store import Store

                store = Store()
                row = store.get_draft(int(draft_id))
                if row is not None:
                    base_text = str(row.get("text") or "")
                    proj = str(row.get("project") or "")
                    merged = f"{base_text}\nПравка владельца: {body.strip()}"
                    asyncio.create_task(
                        _draft_bg(msg.bot, chat, proj, merged, now))
                    return
        # H15: ждём текст задачи после /new (выбор проекта → текст).
        pend = _NEW_PENDING.get(chat)
        if pend is not None and pend.get("stage") == "text" and pend.get("project"):
            _NEW_PENDING.pop(chat, None)
            asyncio.create_task(
                _draft_bg(msg.bot, chat, str(pend["project"]), body.strip(), now))
            return
        text = await asyncio.to_thread(_sync_owner_text, chat, body, now)
        await msg.answer(bc.clip(text))

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def _outbox_loop(bot) -> None:
    """Раз в 5 с слать outbox (шаг — outbox_once)."""
    from hub import time as ht

    log = logging.getLogger("hub.bot")
    state = BotState()
    while True:
        await asyncio.sleep(OUTBOX_S)
        try:
            await outbox_once(bot, state, ht.now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл не умирает
            log.warning("outbox-цикл: %s", e)


async def _poll_loop(bot) -> None:
    """Раз в 15 с: вопросы, продюсер, группировка, авторостер."""
    from hub import time as ht

    log = logging.getLogger("hub.bot")
    state = BotState()
    while True:
        await asyncio.sleep(POLL_S)
        try:
            await poll_once(bot, state, ht.now_ms())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — цикл не умирает
            log.warning("poll-цикл: %s", e)


async def _amain() -> None:
    """Запустить polling + фоны, падение сети — повтор с паузой."""
    from hub.secrets import TELEGRAM_BOT_TOKEN

    log = setup_logging()
    try:
        session = build_session()
        from aiogram import Bot

        bot = Bot(token=TELEGRAM_BOT_TOKEN, session=session)
    except Exception:
        # Стартовый отказ — в bot.log с трейсом, а не молча.
        log.exception("старт бота невозможен")
        raise
    dp = build_dispatcher()
    tasks = [asyncio.create_task(_outbox_loop(bot)),
             asyncio.create_task(_poll_loop(bot))]
    try:
        while True:
            try:
                await dp.start_polling(bot)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — процесс не умирает
                log.warning("polling упал, повтор через 5 с: %s", e)
                await asyncio.sleep(5)
    finally:
        for t in tasks:
            t.cancel()
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001 — закрытие best-effort
            pass


def main() -> int:
    """Точка входа hub bot (блокирует до Ctrl-C)."""
    asyncio.run(_amain())
    return 0
