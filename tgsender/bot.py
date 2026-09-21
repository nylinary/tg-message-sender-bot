from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telethon import TelegramClient
from telethon.extensions import html as tl_html

from . import collect as collect_mod
from .config import Config
from .db import DB
from .scheduling import BADGE, IMPOSSIBLE, build_plan, describe, humanize
from .sender import SendWorker

router = Router()


@dataclass
class Panel:
    cfg: Config
    db: DB
    client: TelegramClient
    worker: SendWorker
    scanning: bool = False
    scan_counts: dict = field(default_factory=dict)


class Compose(StatesGroup):
    waiting_text = State()
    waiting_age = State()
    confirming = State()


# --------------------------------------------------------------------------- #
# keyboards
# --------------------------------------------------------------------------- #


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


async def main_menu(panel: Panel) -> InlineKeyboardMarkup:
    if panel.worker.running:
        return kb(
            [("📊 Проверить статус", "status")],
            [("⏹ Остановить рассылку", "stop")],
        )
    rows = [[("📝 Новая рассылка", "new")]]
    resumable = await panel.db.resumable_campaign()
    if resumable:
        rows.append(
            [(f"▶️ Продолжить #{resumable['id']} ({resumable['pending_n']} осталось)",
              "resume")]
        )
    rows += [[("📊 Статус", "status")], [("🔄 Пересканировать диалоги", "rescan")]]
    return kb(*rows)


def age_keyboard(cfg: Config, counts: dict[str | int, int]) -> InlineKeyboardMarkup:
    rows = []
    for years in cfg.age_options:
        label = f"{years} год" if years == 1 else f"{years} года" if years < 5 else f"{years} лет"
        rows.append([(f"⏱ До {label} — {counts.get(years, 0)} чел.", f"age:{years}")])
    rows.append([(f"♾ Все диалоги — {counts.get('all', 0)} чел.", "age:all")])
    rows.append([("◀️ Отмена", "cancel")])
    return kb(*rows)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


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


def age_from_callback(data: str) -> float | None:
    raw = data.split(":", 1)[1]
    return None if raw == "all" else float(raw)


async def ensure_fresh(panel: Panel, status_msg: Message) -> None:
    """Rescan dialogues if the cache is empty or stale."""
    last = await panel.db.last_collected_at()
    fresh = last is not None and (time.time() - last) < panel.cfg.stale_after_hours * 3600
    if fresh:
        return
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
        counts = await collect_mod.collect(panel.client, panel.db, progress=progress)
        panel.scan_counts = counts
    finally:
        panel.scanning = False


async def status_text(panel: Panel) -> str:
    snap = await panel.worker.snapshot()
    if not snap.get("campaign_id"):
        total = await panel.db.recipients_total()
        last = await panel.db.last_collected_at()
        when = (
            datetime.fromtimestamp(last).strftime("%d.%m %H:%M") if last else "никогда"
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
        head,
        "",
        f"<code>{bar(sent + skipped + failed, total)}</code>",
        f"Отправлено: <b>{sent}</b> из <b>{total}</b>",
        f"Осталось: <b>{pending}</b>",
    ]
    if skipped:
        lines.append(f"Пропущено (закрыты настройками): {skipped}")
    if failed:
        lines.append(f"Ошибок: {failed}")

    lines.append("")
    lines.append(f"За последний час: <b>{snap['last_hour']}</b>")

    if snap["delay"]:
        lines.append(f"Интервал: ~{humanize(snap['delay'] * snap['multiplier'])}")
    if snap["multiplier"] > 1.05:
        lines.append(
            f"⚠️ Замедление ×{snap['multiplier']:.1f} после FLOOD_WAIT от Telegram"
        )

    if snap["running"] and pending:
        eta = pending * snap["delay"] * snap["multiplier"]
        finish = datetime.fromtimestamp(time.time() + eta)
        lines.append(f"Закончит примерно: <b>{finish:%d.%m %H:%M}</b>")
        if finish > panel.cfg.deadline:
            lines.append(
                f"⛔️ Это позже дедлайна {panel.cfg.deadline:%d.%m %H:%M}"
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
# handlers
# --------------------------------------------------------------------------- #


@router.message(Command("start", "menu", "cancel"))
async def cmd_start(message: Message, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    running = panel.worker.running
    total = await panel.db.recipients_total()
    await message.answer(
        "👋 <b>Панель рассылки приглашений</b>\n\n"
        f"Аккаунт-отправитель: <code>{panel.cfg.account}</code>\n"
        f"Диалогов в базе: <b>{total}</b>\n"
        f"Дедлайн: <b>{panel.cfg.deadline:%d.%m.%Y %H:%M}</b>\n\n"
        + ("🟢 Сейчас идёт рассылка." if running else "Готов к работе."),
        reply_markup=await main_menu(panel),
    )


@router.callback_query(F.data == "menu")
async def cb_menu(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    await cq.answer()
    await safe_edit(
        cq.message, "Главное меню.", await main_menu(panel)
    )


@router.callback_query(F.data == "cancel")
async def cb_cancel(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    await cq.answer("Отменено")
    await safe_edit(cq.message, "Отменено.", await main_menu(panel))


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
    await state.set_state(Compose.waiting_text)
    await safe_edit(
        cq.message,
        "📝 Пришли следующим сообщением текст приглашения.\n\n"
        "Он уйдёт всем получателям <b>как есть</b>, без подстановки имён. "
        "Форматирование и ссылки сохранятся.",
        kb([("◀️ Отмена", "cancel")]),
    )


@router.message(Compose.waiting_text)
async def on_text(message: Message, state: FSMContext, panel: Panel) -> None:
    if not message.text:
        await message.answer(
            "Нужен именно текст — фото и файлы эта рассылка не отправляет.",
            reply_markup=kb([("◀️ Отмена", "cancel")]),
        )
        return

    text, parse_mode = resolve_parse_mode(message.html_text, message.text)
    await state.update_data(text=text, parse_mode=parse_mode)

    status_msg = await message.answer("🔍 Проверяю список диалогов…")
    await ensure_fresh(panel, status_msg)

    counts: dict[str | int, int] = {"all": await panel.db.count_by_age(None)}
    for years in panel.cfg.age_options:
        counts[years] = await panel.db.count_by_age(years)

    await state.set_state(Compose.waiting_age)
    await safe_edit(
        status_msg,
        "✅ Текст сохранён.\n\n"
        "Кому отправляем? Выбери, насколько свежим должен быть последний диалог:",
        age_keyboard(panel.cfg, counts),
    )


@router.callback_query(Compose.waiting_age, F.data.startswith("age:"))
async def on_age(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await cq.answer()
    years = age_from_callback(cq.data)
    recipients = await panel.db.count_by_age(years)

    plan = build_plan(recipients, panel.cfg.deadline, panel.cfg.pacing, panel.cfg.risk)
    await state.update_data(max_age=years, delay=plan.delay)
    await state.set_state(Compose.confirming)

    data = await state.get_data()
    preview = data["text"]
    if len(preview) > 600:
        preview = preview[:600] + "…"

    scope = "все диалоги" if years is None else f"последний диалог не старше {years:g} г."
    already = await panel.db.already_sent_in_range(years)
    excluded = (
        f"\n<i>Ещё {already} чел. под этот фильтр подходят, но уже получали "
        f"сообщение в прошлых рассылках — им не отправим.</i>\n"
        if already
        else ""
    )
    body = (
        f"<b>Проверь перед запуском</b>\n\n"
        f"Фильтр: {scope}\n"
        f"{excluded}\n"
        f"{describe(plan, panel.cfg.risk)}\n\n"
        f"─────────\n{preview}"
    )

    buttons = []
    if recipients > 0:
        label = "🚀 Запустить" if plan.feasible else "🚀 Запустить (не успеем в срок)"
        buttons.append([(label, "go")])
    buttons.append([("◀️ Назад", "new"), ("❌ Отмена", "cancel")])
    await safe_edit(cq.message, body, kb(*buttons))


@router.callback_query(Compose.confirming, F.data == "go")
async def on_go(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    data = await state.get_data()
    await state.clear()

    if panel.worker.running:
        await cq.answer("Рассылка уже идёт", show_alert=True)
        return

    campaign_id = await panel.db.create_campaign(
        body=data["text"],
        parse_mode=data["parse_mode"],
        max_age_years=data["max_age"],
        created_by=cq.from_user.id,
        delay=data["delay"],
    )
    total = (await panel.db.progress(campaign_id)).get("total", 0)
    panel.worker.start(campaign_id)

    await cq.answer("Запущено")
    await safe_edit(
        cq.message,
        f"🚀 <b>Рассылка #{campaign_id} запущена.</b>\n\n"
        f"Получателей: <b>{total}</b>\n"
        f"Интервал: ~{humanize(data['delay'])}\n\n"
        "Можно закрыть бота — она идёт в фоне. "
        "Нажми «Статус», чтобы посмотреть прогресс.",
        await main_menu(panel),
    )


@router.callback_query(F.data == "resume")
async def cb_resume(cq: CallbackQuery, state: FSMContext, panel: Panel) -> None:
    await state.clear()
    if panel.worker.running:
        await cq.answer("Рассылка уже идёт", show_alert=True)
        return

    campaign = await panel.db.resumable_campaign()
    if campaign is None:
        await cq.answer("Продолжать нечего", show_alert=True)
        await safe_edit(cq.message, "Незавершённых рассылок нет.", await main_menu(panel))
        return

    # Re-derive the pace: the deadline is closer than when it first started.
    remaining = campaign["pending_n"]
    plan = build_plan(remaining, panel.cfg.deadline, panel.cfg.pacing, panel.cfg.risk)
    await panel.db.reopen_campaign(campaign["id"], plan.delay)
    panel.worker.start(campaign["id"])

    await cq.answer("Продолжаю")
    warning = (
        f"\n\n{BADGE[IMPOSSIBLE]} До дедлайна уже не успеть — пойдёт на максимальной "
        f"скорости и всё равно не хватит времени."
        if plan.risk == IMPOSSIBLE
        else ""
    )
    await safe_edit(
        cq.message,
        f"▶️ <b>Рассылка #{campaign['id']} продолжена.</b>\n\n"
        f"Осталось: <b>{remaining}</b>\n"
        f"Новый интервал: ~{humanize(plan.delay)}"
        f"{warning}",
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
    await cq.answer()
    if panel.worker.running:
        await cq.answer("Нельзя сканировать во время рассылки", show_alert=True)
        return
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
