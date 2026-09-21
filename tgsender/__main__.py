from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from telethon import TelegramClient

from . import config as config_mod
from .bot import Panel, router
from .db import DB
from .sender import SendWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tgsender")


async def _connect_userbot(cfg: config_mod.Config, interactive: bool) -> TelegramClient:
    client = TelegramClient(str(cfg.session_path), cfg.api_id, cfg.api_hash)
    if interactive:
        await client.start()
    else:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise SystemExit(
                "The userbot account is not authorised yet.\n"
                "Run:  python -m tgsender login"
            )
    me = await client.get_me()
    log.info(
        "Userbot ready: %s (@%s, id %s)",
        me.first_name or "?",
        me.username or "-",
        me.id,
    )
    return client


async def cmd_login(cfg: config_mod.Config) -> int:
    client = await _connect_userbot(cfg, interactive=True)
    await client.disconnect()
    print(f"\nSession saved to {cfg.session_path}")
    print("Now run:  python -m tgsender run")
    return 0


async def cmd_run(cfg: config_mod.Config) -> int:
    db = DB(cfg.db_path)
    client = await _connect_userbot(cfg, interactive=False)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    log.info("Control bot ready: @%s", me.username)
    log.info("Admins: %s", ", ".join(str(i) for i in sorted(cfg.admin_ids)))

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
        for admin_id in cfg.admin_ids:
            try:
                await bot.send_message(admin_id, text)
            except Exception as exc:  # noqa: BLE001 - a dead admin chat is not fatal
                log.warning("Could not notify %s: %s", admin_id, exc)

    worker = SendWorker(cfg, db, client, on_event=notify)
    panel = Panel(cfg=cfg, db=db, client=client, worker=worker)

    # A campaign left 'running' by a crash or restart is not actually running.
    # Mark it honestly so the panel offers "продолжить" rather than lying.
    stale = db.active_campaign()
    if stale is not None:
        db.finish_campaign(
            stale["id"], "stopped", "Процесс был перезапущен — рассылка не завершена."
        )
        log.warning("Campaign #%s was left running; marked stopped.", stale["id"])

    # Only the whitelist may touch the panel. Everyone else is ignored silently.
    allowed = set(cfg.admin_ids)
    router.message.filter(F.from_user.id.in_(allowed))
    router.callback_query.filter(F.from_user.id.in_(allowed))

    dp = Dispatcher()
    dp.include_router(router)

    try:
        await dp.start_polling(bot, panel=panel, handle_signals=False)
    finally:
        worker.request_stop()
        if worker.task is not None:
            worker.task.cancel()
            await asyncio.gather(worker.task, return_exceptions=True)
        await bot.session.close()
        await client.disconnect()
        db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tgsender",
        description="Private control bot that mails a party invite from your own "
        "Telegram account to everyone you already have a dialogue with.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="authorise the sending account (run this once)")
    sub.add_parser("run", help="start the control bot and the sender")

    args = parser.parse_args(argv)
    cfg = config_mod.load()
    handler = {"login": cmd_login, "run": cmd_run}[args.command]
    try:
        return asyncio.run(handler(cfg))
    except KeyboardInterrupt:
        print("\nStopped. Progress is saved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
