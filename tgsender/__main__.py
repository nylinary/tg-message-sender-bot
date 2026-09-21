from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from telethon import TelegramClient
from telethon.sessions import StringSession

from . import config as config_mod
from . import settings as settings_mod
from .bot import AdminGate, Panel, router
from .db import DB
from .sender import SendWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgsender")


def _make_client(cfg: config_mod.Config) -> TelegramClient:
    """Session string if we have one, file on disk otherwise.

    Deployed containers have no persistent disk, so TG_SESSION is the only
    thing that survives a redeploy. Locally the file is more convenient.
    """
    session = (
        StringSession(cfg.session_string)
        if cfg.session_string
        else str(cfg.session_path)
    )
    return TelegramClient(session, cfg.api_id, cfg.api_hash)


async def _connect_userbot(cfg: config_mod.Config) -> TelegramClient:
    client = _make_client(cfg)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(
            "The sending account is not authorised.\n"
            "Run `python -m tgsender session` locally, then set TG_SESSION."
        )
    me = await client.get_me()
    if me.bot:
        await client.disconnect()
        raise SystemExit(
            f"TG_SESSION is a BOT session (@{me.username}), not a personal account.\n"
            f"A bot cannot list your dialogues and cannot message anyone who has "
            f"not written to it first, so it can never send the invites.\n"
            f"Re-run `python -m tgsender session` and enter your phone number."
        )
    log.info(
        "Userbot ready: %s (@%s, id %s)", me.first_name or "?", me.username or "-", me.id
    )
    return client


async def _resolve_admin_usernames(client: TelegramClient, gate: AdminGate) -> None:
    """Turn @usernames into permanent numeric ids using the sending account.

    A username can be released and claimed by somebody else; an id cannot.
    Resolving once at startup means the rest of the run compares ids.
    """
    from telethon.tl.types import User

    for name in sorted(gate.usernames):
        try:
            entity = await client.get_entity(name)
        except Exception as exc:  # noqa: BLE001 - one bad handle must not block boot
            log.warning(
                "Admin @%s could not be resolved (%s). Falling back to matching "
                "by username, which is weaker — put their numeric id in "
                "ADMIN_IDS instead.",
                name,
                type(exc).__name__,
            )
            continue
        if not isinstance(entity, User):
            log.warning("Admin @%s is not a user account; ignoring.", name)
            continue
        gate.note_resolved(name, entity.id)
        log.info("Admin @%s -> id %s", name, entity.id)


async def cmd_session(cfg: config_mod.Config) -> int:
    """Log in interactively and print a session string to paste into TG_SESSION."""
    print("=" * 72)
    print("Authorising the account the INVITES WILL BE SENT FROM.")
    print("That is your personal account — the one with all the dialogues.")
    print("This is NOT the bot: do not paste a @BotFather token here.")
    print("=" * 72 + "\n")

    client = TelegramClient(StringSession(), cfg.api_id, cfg.api_hash)
    await client.start(
        phone=lambda: input("Phone number of your personal account (+79991234567): ")
    )
    me = await client.get_me()
    string = client.session.save()
    await client.disconnect()

    if me.bot:
        # A bot cannot list your dialogues and cannot message anyone who has
        # not written to it first, so this session could never send an invite.
        print("\n" + "!" * 72)
        print(f"This logged in as a BOT (@{me.username}), not as a person.")
        print("A bot cannot see your dialogues and cannot start a conversation,")
        print("so it can never deliver the invites.")
        print("")
        print("Run this again and enter your personal PHONE NUMBER at the prompt.")
        print("The bot token belongs in BOT_TOKEN, nowhere else.")
        print("!" * 72)
        return 1

    print("\n" + "=" * 72)
    print(f"Authorised as {me.first_name or ''} (@{me.username or '-'}, id {me.id})")
    print("=" * 72)
    print("\nSet this as TG_SESSION (Railway variable, or your local .env):\n")
    print(string)
    print(
        "\nTreat it exactly like a password — it IS full access to the account.\n"
        "Anyone holding it can read and send your messages. Never commit it.\n"
    )
    return 0


async def cmd_run(cfg: config_mod.Config) -> int:
    db = await DB.connect(config_mod.require_database_url(cfg), cfg.account)
    log.info("Postgres connected; account scope %r", cfg.account)
    guessed = await db.backfill_gender()
    if guessed:
        log.info("Guessed gender for %s recipients collected earlier", guessed)
    s = await settings_mod.load(db, cfg)
    log.info(
        "Settings: deadline %s, interval %ss ±%d%%, timezone %s (local now %s)",
        s.deadline.strftime("%d.%m.%Y %H:%M"),
        f"{s.interval:g}",
        round(s.jitter * 100),
        s.timezone,
        s.now().strftime("%H:%M"),
    )

    client = await _connect_userbot(cfg)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    log.info("Control bot ready: @%s", me.username)
    gate = AdminGate(cfg.admin_ids, cfg.admin_usernames)
    await _resolve_admin_usernames(client, gate)
    log.info("Admins: %s", gate.describe())

    async def notify(event: str, **kw) -> None:
        """Push campaign lifecycle events to everyone with panel access."""
        if event == "finished":
            icon = {"done": "✅", "stopped": "⏹", "failed": "⚠️"}.get(kw["status"], "ℹ️")
            text = f"{icon} Рассылка #{kw['campaign_id']}: <b>{kw['status']}</b>"
            if kw.get("reason"):
                text += f"\n\n{kw['reason']}"
        elif event == "flood":
            text = (
                f"⏳ Telegram попросил подождать {kw['seconds']} с. "
                f"Замедляюсь ×{kw['multiplier']:.1f} и продолжаю."
            )
        else:
            return
        for admin_id in sorted(gate.ids):
            try:
                await bot.send_message(admin_id, text)
            except Exception as exc:  # noqa: BLE001 - a dead admin chat is not fatal
                log.warning("Could not notify %s: %s", admin_id, exc)

    worker = SendWorker(cfg, db, client, on_event=notify)
    panel = Panel(cfg=cfg, db=db, client=client, worker=worker)

    # A campaign the database still calls 'running' is not running — this
    # process just started. Recover it honestly so the panel offers Продолжить
    # instead of claiming a send is in progress.
    stale = await db.active_campaign()
    if stale is not None:
        freed = await db.release_inflight(stale["id"])
        await db.finish_campaign(
            stale["id"],
            "stopped",
            "Процесс был перезапущен (деплой или рестарт) — рассылка не завершена. "
            "Нажми «Продолжить», чтобы дослать оставшимся.",
        )
        log.warning(
            "Campaign #%s was left running; marked stopped, %s in-flight requeued.",
            stale["id"],
            freed,
        )
        for admin_id in sorted(gate.ids):
            try:
                await bot.send_message(
                    admin_id,
                    f"♻️ Бот перезапущен. Рассылка #{stale['id']} была прервана — "
                    f"прогресс сохранён в базе. Нажми «▶️ Продолжить», "
                    f"чтобы дослать оставшимся.",
                )
            except Exception:  # noqa: BLE001
                pass

    # Only the whitelist may touch the panel. Everyone else is ignored silently.
    router.message.filter(gate)
    router.callback_query.filter(gate)

    dp = Dispatcher()
    dp.include_router(router)

    @dp.update.outer_middleware()
    async def log_unrouted(handler, update, data):
        # The router only listens to messages and button presses. Anything
        # else (someone blocking the bot, an edited message) is named here
        # instead of surfacing as a bare "Update is not handled".
        kind = update.event_type
        if kind not in ("message", "callback_query"):
            log.info("Update %s of type %r is not used by this bot", update.update_id, kind)
        return await handler(update, data)

    try:
        await dp.start_polling(bot, panel=panel, handle_signals=False)
    finally:
        worker.request_stop()
        if worker.task is not None:
            worker.task.cancel()
            await asyncio.gather(worker.task, return_exceptions=True)
        await bot.session.close()
        await client.disconnect()
        await db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tgsender",
        description="Private control bot that mails a party invite from your own "
        "Telegram account to everyone you already have a dialogue with.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "session", help="log in and print a TG_SESSION string (run this locally, once)"
    )
    sub.add_parser("run", help="start the control bot and the sender")

    args = parser.parse_args(argv)
    cfg = config_mod.load()
    handler = {"session": cmd_session, "run": cmd_run}[args.command]
    try:
        return asyncio.run(handler(cfg))
    except KeyboardInterrupt:
        print("\nStopped. Progress is in Postgres.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
