"""Checks the aiogram/Telethon wiring without touching the network."""
import asyncio, os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.update(TG_API_ID="1", TG_API_HASH="x" * 32, BOT_TOKEN="123:ABC",
                  ADMIN_IDS="111, 222", TG_ACCOUNT="wiring")

from aiogram import Dispatcher, F
from tgsender import config as config_mod
from tgsender.bot import Panel, router, main_menu, status_text, age_keyboard, kb
from tgsender.db import DB, Recipient
from tgsender.sender import SendWorker

ok = lambda m: print(f"  ok  {m}")
cfg = config_mod.load()
assert cfg.admin_ids == frozenset({111, 222}), "ADMIN_IDS must tolerate spaces"
ok(f"config parses; admins={sorted(cfg.admin_ids)}, deadline={cfg.deadline}")

db = DB(Path(tempfile.mkdtemp()) / "w.db")
YEAR = 365.25 * 86400
for i in range(1, 40):
    db.upsert_recipient(Recipient(i, i * 7, f"u{i}", f"Имя{i}", None,
                                  time.time() - (i / 12) * YEAR))
db.commit()

worker = SendWorker(cfg, db, client=None)
panel = Panel(cfg=cfg, db=db, client=None, worker=worker)

# Dispatcher registration is where aiogram validates handler signatures/filters.
allowed = set(cfg.admin_ids)
router.message.filter(F.from_user.id.in_(allowed))
router.callback_query.filter(F.from_user.id.in_(allowed))
dp = Dispatcher()
dp.include_router(router)
handlers = len(router.message.handlers) + len(router.callback_query.handlers)
ok(f"dispatcher accepts the router; {handlers} handlers registered")

# Every callback_data the keyboards emit must have a handler that claims it.
emitted = set()
for markup in (main_menu(panel),
               age_keyboard(cfg, {1: 5, 2: 9, 3: 14, "all": 39}),
               kb([("x", "status")], [("y", "stop")])):
    for row in markup.inline_keyboard:
        for btn in row:
            emitted.add(btn.callback_data)
known = {"new", "resume", "status", "rescan", "stop", "menu", "cancel", "go"}
known |= {f"age:{y}" for y in cfg.age_options} | {"age:all"}
assert emitted <= known, f"keyboard emits unhandled callbacks: {emitted - known}"
ok(f"every button maps to a handler: {sorted(emitted)}")

# main_menu must change shape as state changes.
assert not any("Продолжить" in b.text for r in main_menu(panel).inline_keyboard for b in r)
cid = db.create_campaign("t", None, 3.0, 111, 30.0)
db.mark(cid, db.next_pending(cid).user_id, "sent")
db.finish_campaign(cid, "stopped", "manual")
assert any("Продолжить" in b.text for r in main_menu(panel).inline_keyboard for b in r)
ok("resume button appears only when a stopped campaign has people left")

# status_text over every lifecycle state, including before any campaign exists.
fresh_panel = Panel(cfg=cfg, db=DB(Path(tempfile.mkdtemp()) / "e.db"), client=None,
                    worker=SendWorker(cfg, DB(Path(tempfile.mkdtemp()) / "e2.db"), None))
assert "Рассылок пока не было" in status_text(fresh_panel)
worker.campaign_id = cid
for state in ("running", "done", "stopped", "failed"):
    db.conn.execute("UPDATE campaigns SET status=? WHERE id=?", (state, cid))
    db.conn.commit()
    text = status_text(panel)
    assert len(text) > 40 and "None" not in text, (state, text)
ok("status renders for every campaign state without leaking None")

# The snapshot the panel reads must survive a worker that never ran.
snap = SendWorker(cfg, db, None).snapshot()
assert snap == {"running": False}
ok("snapshot is safe before a campaign starts")

# The stop event must interrupt a long sleep promptly.
async def stop_wakes_sleeper():
    w = SendWorker(cfg, db, None)
    started = time.monotonic()
    task = asyncio.create_task(w._sleep(30, "test"))
    await asyncio.sleep(0.05)
    w.request_stop()
    result = await task
    assert result is False, "sleep must report that it was interrupted"
    assert time.monotonic() - started < 1.0, "stop must not wait out the full delay"
asyncio.run(stop_wakes_sleeper())
ok("pressing Stop interrupts a pending inter-message delay immediately")

print("\nWIRING OK")
