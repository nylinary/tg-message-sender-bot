"""Checks the aiogram/Telethon/Postgres wiring without touching Telegram.

    DATABASE_URL=postgresql://... .venv/bin/python tests/test_wiring.py
"""
import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("DATABASE_URL", "").strip()
if not DSN:
    print("SKIP: DATABASE_URL not set")
    raise SystemExit(0)

os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("ADMIN_IDS", "111, 222")
os.environ["TG_ACCOUNT"] = "wiring"

from aiogram import Dispatcher  # noqa: E402

from tgsender import config as config_mod  # noqa: E402
from tgsender.bot import (  # noqa: E402
    AdminGate, Panel, age_keyboard, kb, main_menu, router, status_text,
)
from tgsender.db import DB, Recipient  # noqa: E402
from tgsender.sender import SendWorker  # noqa: E402

ok = lambda m: print(f"  ok  {m}")
cfg = config_mod.load()
YEAR = 365.25 * 86400
ACCOUNT = f"wiring_{uuid.uuid4().hex[:8]}"


async def main() -> None:
    db = await DB.connect(config_mod.normalize_dsn(DSN), ACCOUNT)
    try:
        await db.upsert_recipients([
            Recipient(i, i * 7, f"u{i}", f"Имя{i}", None, time.time() - (i / 12) * YEAR)
            for i in range(1, 40)
        ])

        worker = SendWorker(cfg, db, client=None)
        panel = Panel(cfg=cfg, db=db, client=None, worker=worker)

        # Dispatcher registration is where aiogram validates handlers/filters.
        gate = AdminGate(cfg.admin_ids, cfg.admin_usernames)
        router.message.filter(gate)
        router.callback_query.filter(gate)
        dp = Dispatcher()
        dp.include_router(router)
        n = len(router.message.handlers) + len(router.callback_query.handlers)
        ok(f"dispatcher accepts the router; {n} handlers registered")

        # Every callback_data a keyboard emits must have a handler claiming it.
        emitted = set()
        for markup in (await main_menu(panel),
                       age_keyboard(cfg, {1: 5, 2: 9, 3: 14, "all": 39}),
                       kb([("x", "status")], [("y", "stop")])):
            for row in markup.inline_keyboard:
                for btn in row:
                    emitted.add(btn.callback_data)
        known = {"new", "resume", "status", "rescan", "stop", "menu", "cancel", "go"}
        known |= {f"age:{y}" for y in cfg.age_options} | {"age:all"}
        assert emitted <= known, f"unhandled callbacks: {emitted - known}"
        ok(f"every button maps to a handler: {sorted(emitted)}")

        def has_resume(markup):
            return any("Продолжить" in b.text for r in markup.inline_keyboard for b in r)

        assert not has_resume(await main_menu(panel))
        cid = await db.create_campaign("t", None, 3.0, 111, 30.0)
        claimed = await db.claim_next(cid)
        await db.mark(cid, claimed.user_id, "sent")
        await db.release_inflight(cid)
        await db.finish_campaign(cid, "stopped", "manual")
        assert has_resume(await main_menu(panel))
        ok("resume button appears only when a stopped campaign has people left")

        empty = DB(db.pool, f"empty_{uuid.uuid4().hex[:8]}")
        blank_panel = Panel(cfg=cfg, db=empty, client=None,
                            worker=SendWorker(cfg, empty, None))
        assert "Рассылок пока не было" in await status_text(blank_panel)

        worker.campaign_id = cid
        for state in ("running", "done", "stopped", "failed"):
            await db.pool.execute(
                "UPDATE campaigns SET status=$1 WHERE id=$2", state, cid
            )
            text = await status_text(panel)
            assert len(text) > 40 and "None" not in text, (state, text)
        ok("status renders for every campaign state without leaking None")

        assert await SendWorker(cfg, db, None).snapshot() == {"running": False}
        ok("snapshot is safe before a campaign starts")

        # Stop must interrupt a long inter-message sleep promptly.
        w = SendWorker(cfg, db, None)
        started = time.monotonic()
        task = asyncio.create_task(w._sleep(30, "test"))
        await asyncio.sleep(0.05)
        w.request_stop()
        assert await task is False, "sleep must report it was interrupted"
        assert time.monotonic() - started < 1.0, "stop must not wait out the delay"
        ok("pressing Stop interrupts a pending inter-message delay immediately")

    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM deliveries WHERE campaign_id IN "
                "(SELECT id FROM campaigns WHERE account LIKE $1)", "wiring_%"
            )
            await conn.execute("DELETE FROM campaigns WHERE account LIKE $1", "wiring_%")
            await conn.execute("DELETE FROM recipients WHERE account LIKE $1", "wiring_%")
        await db.close()


asyncio.run(main())
print("\nWIRING OK")
