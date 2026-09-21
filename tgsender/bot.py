from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient
from telethon.extensions import html as tl_html

from . import collect as collect_mod
from . import settings as settings_mod
from .config import Config
from .db import DB
from .filters import PERIOD_HELP, PRESETS, Filter, PeriodError, gender_button, parse_period, preset
from .gender import FEMALE, ICONS, MALE, UNKNOWN
from .scheduling import describe, estimate, humanize
from .sender import SendWorker
from .spamcheck import OK, SpamStatus
from .settings import (
    INTERVAL_PRESETS,
    JITTER_PRESETS,
    TIMEZONE_PRESETS,
    SettingError,
)

log = logging.getLogger("tgsender.bot")
router = Router()

DEFAULT_PRESET = "1y"


class AdminGate(BaseFilter):
    """Allows the panel through for a set of numeric ids and/or @usernames.

    Ids are authoritative. Usernames are a convenience that the startup
    resolver turns into ids; whatever is left unresolved is still matched by
    name, and the id is pinned the first time that person appears, so the weak
    check happens at most once per admin per process.
    """

    def __init__(self, ids: Iterable[int], usernames: Iterable[str]):
        self.ids: set[int] = set(ids)
        self.usernames: set[str] = {u.lstrip("@").lower() for u in usernames}
        self.unresolved: set[str] = set(self.usernames)

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is None:
            log.info("Ignoring %s with no sender", type(event).__name__)
            return False
        if user.id in self.ids:
            return True
        name = (user.username or "").lower()
        if name and name in self.usernames:
            self.ids.add(user.id)
            self.unresolved.discard(name)
            log.info("Admin @%s recognised, id pinned as %s", name, user.id)
            return True
        # Worth seeing in the logs: it separates "a stranger found the bot"
        # from "an admin sent something no step expected".
        log.info(
            "Ignoring %s from non-admin id=%s @%s",
            type(event).__name__,
            user.id,
            user.username or "-",
        )
        return False

    def note_resolved(self, username: str, user_id: int) -> None:
        self.ids.add(user_id)
        self.unresolved.discard(username.lstrip("@").lower())

    def describe(self) -> str:
        parts = [str(i) for i in sorted(self.ids)]
        parts += [f"@{u} (unresolved)" for u in sorted(self.unresolved)]
        return ", ".join(parts)


@dataclass
class Panel:
    cfg: Config
    db: DB
    client: TelegramClient
    worker: SendWorker
    spam: object | None = None      # SpamWatch, absent in tests that don't need it
    scanning: bool = False
    scan_counts: dict = field(default_factory=dict)


class Flow(StatesGroup):
    compose_text = State()    # waiting for the invite text
    filtering = State()       # the filter builder is on screen
    custom_period = State()   # waiting for a typed period
    confirming = State()      # the launch screen is on screen


class SetFlow(StatesGroup):
    deadline = State()
    interval = State()
    jitter = State()
    timezone = State()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


async def safe_edit(msg: Message, text: str, markup=None) -> None:
    """Telegram rejects an edit that changes nothing; that is not an error here."""
    try:
        await msg.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc):
            raise


def resolve_parse_mode(html_text: str, plain: str) -> tuple[str, str | None]:
    """Prefer formatted text, but only if Telethon can actually parse it."""
    if html_text == plain:
        return plain, None
    try:
        tl_html.parse(html_text)
    except Exception:
        return plain, None
    return html_text, "html"


def bar(done: int, total: int, width: int = 16) -> str:
    if total <= 0:
        return "─" * width
    filled = round(width * done / total)
    return "█" * filled + "░" * (width - filled)


async def load_settings(panel: Panel) -> settings_mod.Settings:
    return await settings_mod.load(panel.db, panel.cfg)


def default_filter(now: datetime) -> Filter:
    since, until, label = preset(DEFAULT_PRESET, now)
    return Filter().with_period(since, until, label)


async def current_filter(state: FSMContext, now: datetime) -> Filter:
    raw = (await state.get_data()).get("filter")
    return Filter.from_json(raw) if raw else default_filter(now)


async def ensure_fresh(panel: Panel, status_msg: Message) -> None:
    """Rescan dialogues if the cache is empty or stale."""
    last = await panel.db.last_collected_at()
    fresh = last is not None and (time.time() - last) < panel.cfg.stale_after_hours * 3600
    if not fresh:
        await run_scan(panel, status_msg)


async def run_scan(panel: Panel, status_msg: Message) -> None:
    if panel.scanning:
        return
    panel.scanning = True
    last_render = 0.0

    async def progress(counts: dict) -> None:
        nonlocal last_render
        if time.time() - last_render < 2:  # Telegram rate-limits edits
            return
        last_render = time.time()
        await safe_edit(
            status_msg,
            f"🔍 Сканирую диалоги…\n\n"
            f"Просмотрено: <b>{counts['scanned']}</b>\n"
            f"Личных: <b>{counts['saved']}</b>  ·  пропущено: {counts['skipped']}",
        )

    try:
        panel.scan_counts = await collect_mod.collect(
            panel.client, panel.db, progress=progress
        )
    finally:
        panel.scanning = False


# --------------------------------------------------------------------------- #
# main menu
# --------------------------------------------------------------------------- #


async def main_menu(panel: Panel) -> InlineKeyboardMarkup:
    if panel.worker.running:
        return kb(
            [("📊 Проверить статус", "status")],
            [("⏹ Остановить рассылку", "stop")],
            [("🔎 Посчитать получателей", "count")],
            [("🛡 Проверить аккаунт (@SpamBot)", "spam")],
            [("⚙️ Настройки", "set")],
        )
    rows = [[("📝 Новая рассылка", "new")], [("🔎 Посчитать получателей", "count")]]
    resumable = await panel.db.resumable_campaign()
    if resumable:
        rows.append(
            [(f"▶️ Продолжить #{resumable['id']} ({resumable['pending_n']} осталось)",
              "resume")]
        )
    rows += [
        [("📊 Статус", "status"), ("⚙️ Настройки", "set")],
        [("🛡 Проверить аккаунт (@SpamBot)", "spam")],
        [("🔄 Пересканировать диалоги", "rescan")],
    ]
    return kb(*rows)


async def home_text(panel: Panel) -> str:
    s = await load_settings(panel)
    total = await panel.db.recipients_total()
    return (
        "👋 <b>Панель рассылки приглашений</b>\n\n"
        f"Аккаунт-отправитель: <code>{panel.cfg.account}</code>\n"
        f"Диалогов в базе: <b>{total}</b>\n"
        f"🏁 Дедлайн: <b>{s.deadline:%d.%m.%Y %H:%M}</b>\n"
        f"⏱ Интервал: <b>{humanize(s.interval)}</b> ±{s.jitter * 100:.0f}%\n"
        f"🌙 Ночью: {night_text(panel, s)}\n"
        f"🌍 {s.timezone}, сейчас {s.now():%H:%M}\n"
        f"{await spam_line(panel, s)}\n\n"
        + ("🟢 Сейчас идёт рассылка." if panel.worker.running else "Готов к работе.")
    )


async def spam_line(panel: Panel, s: settings_mod.Settings) -> str:
    last = SpamStatus.from_json((await panel.db.get_settings()).get("spam_last"))
    if last is None:
        return "🛡 @SpamBot: ещё не проверяли"
    when = datetime.fromtimestamp(last.checked_at, s.tz).strftime("%d.%m %H:%M")
    return f"🛡 @SpamBot: {last.icon} {last.headline()} <i>({when})</i>"


# --------------------------------------------------------------------------- #
# the filter builder — shared by 🔎 Посчитать and 📝 Новая рассылка
# --------------------------------------------------------------------------- #


async def builder_view(panel: Panel, state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    s = await load_settings(panel)
    data = await state.get_data()
    flt = await current_filter(state, s.now())
    mode = data.get("mode", "count")

    by_gender = await panel.db.breakdown(flt)
    in_period = sum(by_gender.values())
    selected = sum(by_gender.get(g, 0) for g in flt.genders)
    already = await panel.db.already_sent_in_range(flt)

    title = (
        "📝 <b>Кому отправляем?</b>" if mode == "campaign"
        else "🔎 <b>Сколько получателей</b>"
    )
    lines = [
        title,
        "",
        f"📅 Последний диалог: <b>{flt.label}</b>",
        f"🚻 Пол: <b>{flt.genders_text()}</b>",
        "",
        f"За этот период: {in_period}",
        f"    ♀ {by_gender.get(FEMALE, 0)}  ·  ♂ {by_gender.get(MALE, 0)}"
        f"  ·  ❔ {by_gender.get(UNKNOWN, 0)}",
        f"👥 <b>Выбрано: {selected}</b>",
    ]
    if already:
        lines.append(
            f"\n<i>Ещё {already} под фильтр подходят, но уже получали приглашение — "
            f"им не отправим.</i>"
        )
    lines.append(
        "\n<i>Пол угадан по имени и фамилии — ошибки будут. "
        "«👀 Кто попал» покажет примеры.</i>"
    )

    def period_button(key: str, button: str, description: str) -> tuple[str, str]:
        mark = "• " if flt.label == description else ""
        return (f"{mark}{button}", f"f:p:{key}")

    presets = [period_button(k, b, d) for k, b, d, _ in PRESETS]
    rows = [
        presets[:3],
        presets[3:],
        [("✏️ Свой период / даты", "f:custom")],
        [(gender_button(flt, g), f"f:g:{g}") for g in (FEMALE, MALE, UNKNOWN)],
        [("👀 Кто попал", "f:who")],
    ]
    if mode == "campaign":
        rows.append([("➡️ Далее", "f:next")])
    else:
        rows.append([("📝 Разослать этим людям", "f:use")])
    rows.append([("◀️ Меню", "menu")])
    return "\n".join(lines), kb(*rows)


async def show_builder(msg: Message, panel: Panel, state: FSMContext, *, edit: bool) -> None:
    await state.set_state(Flow.filtering)
    text, markup = await builder_view(panel, state)
    if edit:
        await safe_edit(msg, text, markup)
    else:
        await msg.answer(text, reply_markup=markup)


async def confirm_view(panel: Panel, state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    s = await load_settings(panel)
    data = await state.get_data()
    flt = await current_filter(state, s.now())
    recipients = await panel.db.count(flt)
    already = await panel.db.already_sent_in_range(flt)
    est = estimate(
        recipients, s.interval, s.jitter, s.deadline,
        panel.cfg.pacing, panel.cfg.risk, s.now(), s.pause_at_night,
    )

    preview = data.get("text", "")
    if len(preview) > 600:
        preview = preview[:600] + "…"
    excluded = (
        f"<i>Ещё {already} подходят, но уже получали — им не отправим.</i>\n"
        if already else ""
    )
    body = (
        "<b>Проверь перед запуском</b>\n\n"
        f"Фильтр: {flt.summary()}\n{excluded}\n"
        f"{describe(est, panel.cfg.pacing, panel.cfg.risk, s.timezone, s.pause_at_night)}\n\n"
        f"<i>Интервал, джиттер и дедлайн можно менять в ⚙️ Настройках "
        f"и во время рассылки.</i>\n\n"
        f"─────────\n{preview}"
    )
    rows = []
    if recipients > 0 and est.capacity > 0:
        rows.append([("🚀 Запустить", "go")])
    rows += [[("◀️ Фильтр", "f:back"), ("⚙️ Настройки", "set")], [("❌ Отмена", "cancel")]]
    return body, kb(*rows)


# --------------------------------------------------------------------------- #
# settings screen
# --------------------------------------------------------------------------- #


async def settings_view(panel: Panel) -> tuple[str, InlineKeyboardMarkup]:
    s = await load_settings(panel)
    p = panel.cfg.pacing
    lo, hi = s.gap_range
    left = (s.deadline - s.now()).total_seconds()
    until = f"через {humanize(left)}" if left > 0 else "⛔️ уже прошёл"
    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🏁 Дедлайн: <b>{s.deadline:%d.%m.%Y %H:%M}</b> ({until})\n"
        f"⏱ Интервал: <b>{humanize(s.interval)}</b> (~{3600 / s.interval:.0f}/час)\n"
        f"🎲 Джиттер: <b>±{s.jitter * 100:.0f}%</b> → каждая пауза от "
        f"{humanize(lo)} до {humanize(hi)}\n"
        f"🌍 Часовой пояс: <b>{s.timezone}</b> (сейчас {s.now():%d.%m %H:%M})\n"
        f"🌙 Ночью: <b>{night_text(panel, s)}</b>\n"
        f"🛡 Автопроверка @SpamBot: <b>{spam_schedule_text(s)}</b>\n\n"
        "<i>Изменения действуют сразу, в том числе на идущую рассылку. "
        "После дедлайна отправка останавливается.</i>"
    )
    return text, kb(
        [("🏁 Дедлайн", "set:dl"), ("⏱ Интервал", "set:int")],
        [("🎲 Джиттер", "set:jit"), ("🌍 Часовой пояс", "set:tz")],
        [("🛡 Автопроверка @SpamBot", "set:spam")],
        [(
            "🌙 Переключить: ночью не отправлять" if not s.pause_at_night
            else "🌙 Переключить: ночью слать без звука",
            "set:night",
        )],
        [("◀️ Меню", "menu")],
    )


SETTING_PROMPTS = {
    "dl": (
        SetFlow.deadline,
        "🏁 <b>Новый дедлайн</b>\n\nНапиши дату и время, например:\n"
        "<code>25.09.2026 21:00</code> или <code>25.09 21:00</code>\n\n"
        "Время — по часовому поясу из настроек. Без времени — до конца дня.",
    ),
    "int": (
        SetFlow.interval,
        "⏱ <b>Свой интервал</b>\n\nНапиши паузу между сообщениями: "
        "<code>180</code> (секунды), <code>3м</code>, <code>2м30с</code>.",
    ),
    "jit": (
        SetFlow.jitter,
        "🎲 <b>Свой джиттер</b>\n\nНапиши процент от 0 до 90, например <code>35</code>.",
    ),
    "tz": (
        SetFlow.timezone,
        "🌍 <b>Свой часовой пояс</b>\n\nНапиши название вроде "
        "<code>Europe/Moscow</code> или смещение <code>UTC+3</code>.",
    ),
}


def interval_keyboard() -> InlineKeyboardMarkup:
    presets = [(humanize(v), f"set:int:{v}") for v in INTERVAL_PRESETS]
    return kb(presets[:3], presets[3:], [("✏️ Своё", "set:int:custom")],
              [("◀️ Настройки", "set")])


def jitter_keyboard() -> InlineKeyboardMarkup:
    presets = [(f"±{v}%", f"set:jit:{v}") for v in JITTER_PRESETS]
    return kb(presets, [("✏️ Своё", "set:jit:custom")], [("◀️ Настройки", "set")])


def timezone_keyboard() -> InlineKeyboardMarkup:
    buttons = [(label, f"set:tz:{zone}") for label, zone in TIMEZONE_PRESETS]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    return kb(*rows, [("✏️ Другой", "set:tz:custom")], [("◀️ Настройки", "set")])


def spam_schedule_text(s: settings_mod.Settings) -> str:
    if s.spam_every_hours <= 0:
        return "выключена"
    notify = (
        "уведомлять всегда" if s.spam_notify == settings_mod.SPAM_NOTIFY_ALWAYS
        else "уведомлять только о проблемах"
    )
    return f"каждые {s.spam_every_hours} ч, {notify}"


def spam_settings_keyboard(s: settings_mod.Settings) -> InlineKeyboardMarkup:
    def label(h: int) -> str:
        text = "Выкл" if h == 0 else f"{h} ч"
        return f"• {text}" if h == s.spam_every_hours else text
    presets = [(label(h), f"set:spam:{h}") for h in settings_mod.SPAM_EVERY_PRESETS]
    notify = (
        "🔔 Сейчас: уведомлять всегда → только о проблемах"
        if s.spam_notify == settings_mod.SPAM_NOTIFY_ALWAYS
        else "🔕 Сейчас: только о проблемах → уведомлять всегда"
    )
    return kb(presets[:3], presets[3:], [(notify, "set:spamnotify")],
              [("🛡 Проверить сейчас", "spam")], [("◀️ Настройки", "set")])


def night_text(panel: Panel, s: settings_mod.Settings) -> str:
    p = panel.cfg.pacing
    hours = f"{p.quiet_start:02d}:00–{p.quiet_end:02d}:00"
    return f"не отправляем ({hours})" if s.pause_at_night else f"без звука ({hours})"


async def save_setting(panel: Panel, key: str, value: str, user_id: int) -> None:
    await panel.db.set_setting(key, value, user_id)
    log.info("Setting %s=%s changed by %s", key, value, user_id)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


async def status_text(panel: Panel) -> str:
    snap = await panel.worker.snapshot()
    s = await load_settings(panel)
    if not snap.get("campaign_id"):
        total = await panel.db.recipients_total()
        last = await panel.db.last_collected_at()
        when = (
            datetime.fromtimestamp(last, s.tz).strftime("%d.%m %H:%M") if last else "никогда"
        )
        return (
            "Рассылок пока не было.\n\n"
            f"В базе <b>{total}</b> личных диалогов.\n"
            f"Последнее сканирование: {when}"
        )

    c = snap["counts"]
    total = c.get("total", 0)
    sent = c.get("sent", 0)
    pending = c.get("pending", 0)
    skipped = c.get("skipped", 0)
    failed = c.get("failed", 0)

    head = {
        "running": "🟢 <b>Идёт рассылка</b>",
        "done": "✅ <b>Рассылка завершена</b>",
        "stopped": "⏹ <b>Рассылка остановлена</b>",
        "failed": "⚠️ <b>Рассылка прервана ошибкой</b>",
    }.get(snap["status"], f"<b>{snap['status']}</b>")

    lines = [
        head + f" #{snap['campaign_id']}",
        f"<i>{Filter.from_json(snap.get('filter_json')).summary()}</i>",
        "",
        f"<code>{bar(sent + skipped + failed, total)}</code>",
        f"Отправлено: <b>{sent}</b> из <b>{total}</b>",
        f"Осталось: <b>{pending}</b>",
    ]
    if skipped:
        lines.append(f"Пропущено (закрыты настройками): {skipped}")
    if failed:
        lines.append(f"Ошибок: {failed}")

    lines += [
        "",
        f"За последний час: <b>{snap['last_hour']}</b>",
        f"Интервал: ~{humanize(s.interval * snap['multiplier'])} ±{s.jitter * 100:.0f}%",
    ]
    if snap["multiplier"] > 1.05:
        lines.append(f"⚠️ Замедление ×{snap['multiplier']:.1f} после FLOOD_WAIT от Telegram")

    if snap["running"] and pending:
        est = estimate(
            pending, s.interval * snap["multiplier"], s.jitter, s.deadline,
            panel.cfg.pacing, panel.cfg.risk, s.now(), s.pause_at_night,
        )
        lines.append(f"Закончит примерно: <b>{est.finishes_at:%d.%m %H:%M}</b>")
        if not est.fits:
            lines.append(
                f"⛔️ К дедлайну {s.deadline:%d.%m %H:%M} не успеет ~{est.missed} чел. "
                f"Уменьши интервал в ⚙️ Настройках."
            )

    if snap["waiting_for"] > 5:
        lines.append(
            f"\n⏸ Сейчас пауза: {humanize(snap['waiting_for'])} ({snap['wait_reason']})"
        )
    if snap["stop_reason"]:
        lines.append(f"\n<i>{snap['stop_reason']}</i>")

    errors = await panel.db.recent_errors(snap["campaign_id"])
    if errors:
        lines.append("\n<i>Последние ошибки:</i>")
        for e in errors:
            lines.append(f"<i>· {e['user_id']}: {e['error']}</i>")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# handlers: menu
# --------------------------------------------------------------------------- #


@router.message(Command("start", "menu", "cancel"))
async def cmd_start(message: Message, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    await message.answer(await home_text(panel), reply_markup=await main_menu(panel))


@router.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    await cq.answer()
    await safe_edit(cq.message, await home_text(panel), await main_menu(panel))


@router.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    await cq.answer("Отменено")
    await safe_edit(cq.message, await home_text(panel), await main_menu(panel))


# --------------------------------------------------------------------------- #
# handlers: count and compose
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "count")
async def cb_count(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    await state.clear()
    await state.update_data(mode="count")
    await safe_edit(cq.message, "🔍 Проверяю список диалогов…")
    await ensure_fresh(panel, cq.message)
    await show_builder(cq.message, panel, state, edit=True)


@router.callback_query(F.data == "new")
async def cb_new(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    if panel.worker.running:
        await safe_edit(
            cq.message,
            "Уже идёт рассылка. Останови её, прежде чем начинать новую.",
            await main_menu(panel),
        )
        return
    await state.clear()
    await state.update_data(mode="campaign")
    await state.set_state(Flow.compose_text)
    await safe_edit(
        cq.message,
        "📝 Пришли следующим сообщением текст приглашения.\n\n"
        "Он уйдёт всем получателям <b>как есть</b>, без подстановки имён. "
        "Форматирование и ссылки сохранятся.",
        kb([("◀️ Отмена", "cancel")]),
    )


@router.message(Flow.compose_text)
async def on_text(message: Message, state: FSMContext, panel: Panel) -> None:
    if not message.text:
        await message.answer(
            "Нужен именно текст — фото и файлы эта рассылка не отправляет.",
            reply_markup=kb([("◀️ Отмена", "cancel")]),
        )
        return
    text, parse_mode = resolve_parse_mode(message.html_text, message.text)
    await state.update_data(text=text, parse_mode=parse_mode, mode="campaign")

    status_msg = await message.answer("🔍 Проверяю список диалогов…")
    await ensure_fresh(panel, status_msg)
    if (await state.get_data()).get("filter"):
        # Came here from 🔎 Посчитать → «Разослать этим людям»: filter is set.
        await state.set_state(Flow.confirming)
        body, markup = await confirm_view(panel, state)
        await safe_edit(status_msg, body, markup)
    else:
        await show_builder(status_msg, panel, state, edit=True)


# --------------------------------------------------------------------------- #
# handlers: the builder
# --------------------------------------------------------------------------- #


@router.callback_query(Flow.filtering, F.data.startswith("f:p:"))
async def on_preset(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    s = await load_settings(panel)
    flt = await current_filter(state, s.now())
    since, until, label = preset(cq.data.split(":", 2)[2], s.now())
    await state.update_data(filter=flt.with_period(since, until, label).to_json())
    await show_builder(cq.message, panel, state, edit=True)


@router.callback_query(Flow.filtering, F.data.startswith("f:g:"))
async def on_gender(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    s = await load_settings(panel)
    flt = await current_filter(state, s.now())
    gender = cq.data.split(":", 2)[2]
    toggled = flt.toggle(gender)
    if toggled.genders == flt.genders:
        await cq.answer("Хотя бы одна группа должна остаться", show_alert=True)
        return
    await cq.answer()
    await state.update_data(filter=toggled.to_json())
    await show_builder(cq.message, panel, state, edit=True)


@router.callback_query(Flow.filtering, F.data == "f:custom")
async def on_custom_period(cq: CallbackQuery, state: FSMContext) -> None:
    await cq.answer()
    await state.set_state(Flow.custom_period)
    await safe_edit(cq.message, PERIOD_HELP, kb([("◀️ Назад", "f:back")]))


@router.message(Flow.custom_period)
async def on_period_text(message: Message, state: FSMContext, panel: Panel) -> None:
    s = await load_settings(panel)
    try:
        since, until, label = parse_period(message.text or "", s.now())
    except PeriodError as exc:
        await message.answer(f"⚠️ {exc}\n\nПопробуй ещё раз или нажми «Назад».",
                             reply_markup=kb([("◀️ Назад", "f:back")]))
        return
    flt = await current_filter(state, s.now())
    await state.update_data(filter=flt.with_period(since, until, label).to_json())
    await show_builder(message, panel, state, edit=False)


@router.callback_query(Flow.filtering, F.data == "f:who")
async def on_who(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    s = await load_settings(panel)
    flt = await current_filter(state, s.now())
    people = await panel.db.sample(flt, 25)
    if not people:
        body = "Под фильтр никто не попал."
    else:
        lines = [f"👀 <b>Случайные {len(people)} из выбранных</b>", ""]
        for r in people:
            when = (
                datetime.fromtimestamp(r.last_message_at, s.tz).strftime("%d.%m.%y")
                if r.last_message_at else "—"
            )
            handle = f" @{r.username}" if r.username else ""
            lines.append(f"{ICONS.get(r.gender or UNKNOWN, '❔')} {r.label}{handle} · {when}")
        lines.append("\n<i>Значок — угаданный пол, дата — последний диалог.</i>")
        body = "\n".join(lines)
    await safe_edit(cq.message, body, kb([("🔄 Другие", "f:who")], [("◀️ К фильтру", "f:back")]))


@router.callback_query(
    StateFilter(Flow.filtering, Flow.custom_period, Flow.confirming), F.data == "f:back"
)
async def on_back_to_builder(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    await show_builder(cq.message, panel, state, edit=True)


@router.callback_query(Flow.filtering, F.data == "f:use")
async def on_use_filter(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    """From 🔎 Посчитать straight into a campaign with the same filter."""
    if panel.worker.running:
        await cq.answer("Сначала останови текущую рассылку", show_alert=True)
        return
    await cq.answer()
    s = await load_settings(panel)
    flt = await current_filter(state, s.now())
    await state.update_data(mode="campaign", filter=flt.to_json())
    await state.set_state(Flow.compose_text)
    await safe_edit(
        cq.message,
        f"📝 Фильтр: <b>{flt.summary()}</b>\n\n"
        "Пришли следующим сообщением текст приглашения. Он уйдёт всем "
        "<b>как есть</b>, форматирование и ссылки сохранятся.",
        kb([("◀️ Отмена", "cancel")]),
    )


@router.callback_query(Flow.filtering, F.data == "f:next")
async def on_next(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    await state.set_state(Flow.confirming)
    body, markup = await confirm_view(panel, state)
    await safe_edit(cq.message, body, markup)


@router.callback_query(Flow.confirming, F.data == "go")
async def on_go(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    if panel.worker.running:
        await cq.answer("Рассылка уже идёт", show_alert=True)
        return
    s = await load_settings(panel)
    data = await state.get_data()
    flt = await current_filter(state, s.now())
    if not data.get("text"):
        await cq.answer("Текст потерялся — начни заново", show_alert=True)
        return
    await state.clear()

    campaign_id = await panel.db.create_campaign(
        body=data["text"],
        parse_mode=data.get("parse_mode"),
        flt=flt,
        created_by=cq.from_user.id,
        interval=s.interval,
    )
    total = (await panel.db.progress(campaign_id)).get("total", 0)
    if total == 0:
        await panel.db.finish_campaign(campaign_id, "done", "Под фильтр никто не попал")
        await cq.answer("Под фильтр никто не попал", show_alert=True)
        return
    panel.worker.start(campaign_id)
    await cq.answer("Запущено")
    await safe_edit(
        cq.message,
        f"🚀 <b>Рассылка #{campaign_id} запущена.</b>\n\n"
        f"Фильтр: {flt.summary()}\n"
        f"Получателей: <b>{total}</b>\n"
        f"Интервал: ~{humanize(s.interval)} ±{s.jitter * 100:.0f}%\n\n"
        "Можно закрыть бота — она идёт в фоне. «Статус» покажет прогресс.",
        await main_menu(panel),
    )


# --------------------------------------------------------------------------- #
# handlers: running campaigns
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "resume")
async def cb_resume(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    if panel.worker.running:
        await cq.answer("Рассылка уже идёт", show_alert=True)
        return
    campaign = await panel.db.resumable_campaign()
    if campaign is None:
        await cq.answer("Продолжать нечего", show_alert=True)
        await safe_edit(cq.message, await home_text(panel), await main_menu(panel))
        return

    s = await load_settings(panel)
    remaining = campaign["pending_n"]
    est = estimate(
        remaining, s.interval, s.jitter, s.deadline,
        panel.cfg.pacing, panel.cfg.risk, s.now(), s.pause_at_night,
    )
    if est.seconds_available <= 0:
        await cq.answer(
            "Дедлайн уже прошёл — сдвинь его в ⚙️ Настройках", show_alert=True
        )
        return
    await panel.db.reopen_campaign(campaign["id"], s.interval)
    panel.worker.start(campaign["id"])

    await cq.answer("Продолжаю")
    late = (
        f"\n\n⛔️ К дедлайну не успеет ~{est.missed} чел. — уменьши интервал в "
        f"⚙️ Настройках, если нужно всем."
        if not est.fits else f"\nЗакончит ~{est.finishes_at:%d.%m %H:%M}."
    )
    await safe_edit(
        cq.message,
        f"▶️ <b>Рассылка #{campaign['id']} продолжена.</b>\n\n"
        f"Осталось: <b>{remaining}</b>\n"
        f"Интервал: ~{humanize(s.interval)} ±{s.jitter * 100:.0f}%{late}",
        await main_menu(panel),
    )


@router.callback_query(F.data == "status")
async def cb_status(cq: CallbackQuery, panel: Panel) -> None:
    await cq.answer()
    await safe_edit(
        cq.message,
        await status_text(panel),
        kb(
            [("🔄 Обновить", "status")],
            *([[("⏹ Остановить", "stop")]] if panel.worker.running else []),
            [("◀️ Меню", "menu")],
        ),
    )


@router.message(Command("status"))
async def cmd_status(message: Message, panel: Panel) -> None:
    await message.answer(
        await status_text(panel),
        reply_markup=kb([("🔄 Обновить", "status")], [("◀️ Меню", "menu")]),
    )


@router.callback_query(F.data == "stop")
async def cb_stop(cq: CallbackQuery, panel: Panel) -> None:
    if not panel.worker.running:
        await cq.answer("Нечего останавливать", show_alert=True)
        return
    panel.worker.request_stop()
    await cq.answer("Останавливаю…")
    await safe_edit(
        cq.message,
        "⏹ Останавливаю. Текущее сообщение дойдёт, дальше рассылка встанет.\n\n"
        "Прогресс сохранён. В главном меню появится кнопка "
        "«▶️ Продолжить» — она допошлёт ровно тем, кто ещё не получил.",
        kb([("📊 Статус", "status")], [("◀️ Меню", "menu")]),
    )


@router.callback_query(F.data == "rescan")
async def cb_rescan(cq: CallbackQuery, panel: Panel) -> None:
    if panel.worker.running:
        await cq.answer("Нельзя сканировать во время рассылки", show_alert=True)
        return
    await cq.answer()
    await safe_edit(cq.message, "🔍 Сканирую диалоги…")
    await run_scan(panel, cq.message)
    c = panel.scan_counts
    await safe_edit(
        cq.message,
        f"✅ Готово.\n\nПросмотрено: <b>{c.get('scanned', 0)}</b>\n"
        f"Личных диалогов: <b>{c.get('saved', 0)}</b>\n"
        f"Пропущено (боты, каналы, группы): {c.get('skipped', 0)}",
        await main_menu(panel),
    )


@router.callback_query(F.data == "spam")
async def cb_spam(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    if panel.spam is None:
        await cq.answer("Проверка недоступна", show_alert=True)
        return
    await cq.answer("Спрашиваю @SpamBot…")
    await state.clear()
    await safe_edit(cq.message, "🛡 Спрашиваю @SpamBot… это занимает несколько секунд.")
    status, previous, stopped = await panel.spam.check()
    report = await panel.spam.report(status, previous, stopped)
    if stopped:
        # A stopped campaign concerns every admin, not just whoever pressed.
        await panel.spam.broadcast(report)
    await safe_edit(
        cq.message,
        report + (
            "\n\n<i>@SpamBot показывает только статус аккаунта. Сколько было жалоб, "
            "Telegram не сообщает.</i>" if status.state == OK else ""
        ),
        kb([("🔄 Проверить ещё раз", "spam")], [("⏰ Автопроверка", "set:spam")],
           [("◀️ Меню", "menu")]),
    )


# --------------------------------------------------------------------------- #
# handlers: settings
# --------------------------------------------------------------------------- #


@router.callback_query(F.data == "set")
async def cb_settings(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    await state.clear()
    text, markup = await settings_view(panel)
    await safe_edit(cq.message, text, markup)


@router.callback_query(F.data.startswith("set:"))
async def cb_setting(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    parts = cq.data.split(":", 2)
    what = parts[1]
    value = parts[2] if len(parts) > 2 else None

    if what == "spam" and value is None:
        await cq.answer()
        s = await load_settings(panel)
        await safe_edit(
            cq.message,
            "🛡 <b>Автопроверка @SpamBot</b>\n\n"
            f"Сейчас: <b>{spam_schedule_text(s)}</b>\n\n"
            "Бот сам пишет @SpamBot с вашего аккаунта и присылает результат всем "
            "админам. Если аккаунт ограничен — идущая рассылка останавливается.\n\n"
            "Как часто проверять?",
            spam_settings_keyboard(s),
        )
        return
    if what == "spam":
        try:
            hours = int(value)
        except ValueError:
            await cq.answer("Неверное значение", show_alert=True)
            return
        await save_setting(panel, "spam_every_hours", str(hours), cq.from_user.id)
        await cq.answer("Выключено" if hours == 0 else f"Каждые {hours} ч")
        s = await load_settings(panel)
        await safe_edit(
            cq.message,
            f"🛡 <b>Автопроверка @SpamBot</b>\n\nСейчас: <b>{spam_schedule_text(s)}</b>",
            spam_settings_keyboard(s),
        )
        return
    if what == "spamnotify":
        s = await load_settings(panel)
        new = (
            settings_mod.SPAM_NOTIFY_PROBLEMS
            if s.spam_notify == settings_mod.SPAM_NOTIFY_ALWAYS
            else settings_mod.SPAM_NOTIFY_ALWAYS
        )
        await save_setting(panel, "spam_notify", new, cq.from_user.id)
        await cq.answer("Сохранено")
        s = await load_settings(panel)
        await safe_edit(
            cq.message,
            f"🛡 <b>Автопроверка @SpamBot</b>\n\nСейчас: <b>{spam_schedule_text(s)}</b>",
            spam_settings_keyboard(s),
        )
        return
    if what == "night":
        s = await load_settings(panel)
        new = settings_mod.NIGHT_SILENT if s.pause_at_night else settings_mod.NIGHT_PAUSE
        await save_setting(panel, "night_mode", new, cq.from_user.id)
        await cq.answer(settings_mod.NIGHT_MODES[new].capitalize())
        text, markup = await settings_view(panel)
        await safe_edit(cq.message, text, markup)
        return
    if value is None and what == "int":
        await cq.answer()
        await safe_edit(cq.message, "⏱ <b>Интервал между сообщениями</b>", interval_keyboard())
        return
    if value is None and what == "jit":
        await cq.answer()
        await safe_edit(
            cq.message,
            "🎲 <b>Джиттер</b>\n\nНа сколько случайно растягивать или сжимать каждую "
            "паузу. Ровный шаг «ровно каждые 180 с» — машинный след, живой человек "
            "так не пишет.",
            jitter_keyboard(),
        )
        return
    if value is None and what == "tz":
        await cq.answer()
        await safe_edit(cq.message, "🌍 <b>Часовой пояс</b>", timezone_keyboard())
        return
    if what == "dl" or value == "custom":
        await cq.answer()
        new_state, prompt = SETTING_PROMPTS[what]
        await state.set_state(new_state)
        await safe_edit(cq.message, prompt, kb([("◀️ Настройки", "set")]))
        return

    try:
        if what == "int":
            stored = str(settings_mod.parse_interval(value, panel.cfg.pacing.min_interval))
        elif what == "jit":
            stored = str(settings_mod.parse_jitter(value))
        elif what == "tz":
            stored = settings_mod.parse_timezone(value)
        else:
            raise SettingError("Неизвестная настройка")
    except SettingError as exc:
        await cq.answer(str(exc), show_alert=True)
        return

    key = {"int": "interval", "jit": "jitter", "tz": "timezone"}[what]
    await save_setting(panel, key, stored, cq.from_user.id)
    await cq.answer("Сохранено")
    text, markup = await settings_view(panel)
    await safe_edit(cq.message, text, markup)


@router.message(StateFilter(SetFlow.deadline, SetFlow.interval, SetFlow.jitter, SetFlow.timezone))
async def on_setting_text(message: Message, state: FSMContext, panel: Panel) -> None:
    current = await state.get_state()
    raw = (message.text or "").strip()
    s = await load_settings(panel)
    try:
        if current == SetFlow.deadline.state:
            key, value = "deadline", settings_mod.parse_deadline(raw, s.now()).isoformat(
                timespec="minutes"
            )
        elif current == SetFlow.interval.state:
            key = "interval"
            value = str(settings_mod.parse_interval(raw, panel.cfg.pacing.min_interval))
        elif current == SetFlow.jitter.state:
            key, value = "jitter", str(settings_mod.parse_jitter(raw))
        else:
            key, value = "timezone", settings_mod.parse_timezone(raw)
    except SettingError as exc:
        await message.answer(f"⚠️ {exc}", reply_markup=kb([("◀️ Настройки", "set")]))
        return

    await save_setting(panel, key, value, message.from_user.id)
    await state.clear()
    text, markup = await settings_view(panel)
    await message.answer("✅ Сохранено.\n\n" + text, reply_markup=markup)


# --------------------------------------------------------------------------- #
# fallbacks — registered last, so they only catch what nothing else claimed
# --------------------------------------------------------------------------- #


@router.callback_query()
async def cb_stale(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    """A button from a message older than the current process.

    FSM state lives in memory, so a redeploy loses it and the step-specific
    handlers stop matching. Without this the button just spins forever.
    """
    await state.clear()
    await cq.answer("Эта кнопка устарела — бот перезапускался", show_alert=True)
    await safe_edit(
        cq.message,
        "♻️ Бот перезапускался, и эта кнопка больше не активна.\n\n"
        "Прогресс рассылок не потерян — он в базе. Начни заново из меню.",
        await main_menu(panel),
    )


@router.message()
async def msg_fallback(message: Message, state: FSMContext, panel: Panel) -> None:
    """Anything an admin sends that no step expected."""
    await state.clear()
    await message.answer(
        "Не понял. Выбери действие в меню.",
        reply_markup=await main_menu(panel),
    )
