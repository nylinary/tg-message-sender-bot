"""Offline tests: no Telegram connection, no network. Run with:

    .venv/bin/python tests/test_offline.py
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("ADMIN_IDS", "111,222")
os.environ["TG_ACCOUNT"] = "selftest"

from tgsender import bot as bot_mod  # noqa: E402
from tgsender import config as config_mod  # noqa: E402
from tgsender import scheduling as sch  # noqa: E402
from tgsender.db import DB, Recipient  # noqa: E402

ok = lambda m: print(f"  ok  {m}")
cfg = config_mod.load()
YEAR = 365.25 * 86400


def fresh_db() -> DB:
    return DB(Path(tempfile.mkdtemp()) / "t.db")


def person(uid: int, years_ago: float) -> Recipient:
    return Recipient(uid, uid * 10, f"u{uid}", f"N{uid}", None, time.time() - years_ago * YEAR)


# --------------------------------------------------------------------------- #
# quiet hours
# --------------------------------------------------------------------------- #

assert sch.is_quiet(2, 23, 10) and sch.is_quiet(23, 23, 10) and sch.is_quiet(9, 23, 10)
assert not sch.is_quiet(10, 23, 10) and not sch.is_quiet(15, 23, 10)
assert sch.is_quiet(3, 1, 6) and not sch.is_quiet(8, 1, 6)
assert not sch.is_quiet(h := 5, 0, 0), "start == end disables quiet hours"
ok("quiet-hour window wraps past midnight and can be disabled")

base = datetime(2026, 9, 21, 12, 0)
assert sch.active_seconds(base, base + timedelta(hours=2), 23, 10) == 7200
assert sch.active_seconds(base, base, 23, 10) == 0
assert sch.active_seconds(base, base - timedelta(hours=5), 23, 10) == 0, "past deadline"
# 12:00 -> 12:00 next day, minus the 23:00-10:00 block = 13 sendable hours
assert sch.active_seconds(base, base + timedelta(days=1), 23, 10) == 13 * 3600
# A partial first hour must be counted as a partial hour, not a whole one.
assert sch.active_seconds(base.replace(minute=30), base + timedelta(hours=1), 23, 10) == 1800
assert sch.active_seconds(base, base + timedelta(days=1), 0, 0) == 86400
ok("active_seconds excludes quiet hours and handles partial hours")

# --------------------------------------------------------------------------- #
# the deadline -> rate plan
# --------------------------------------------------------------------------- #

deadline = base + timedelta(days=4)
total_window = sch.active_seconds(base, deadline, 23, 10)

plan = sch.build_plan(100, deadline, cfg.pacing, cfg.risk, now=base)
assert abs(plan.delay - total_window / 99) < 1e-6, "delay must fill the window exactly"
assert plan.risk == sch.GREEN and plan.feasible and not plan.at_floor
assert plan.finishes_at <= deadline + timedelta(seconds=1), "must land inside the window"
ok(f"100 recipients over 4 days -> {sch.humanize(plan.delay)} apart, green")

small = sch.build_plan(1, deadline, cfg.pacing, cfg.risk, now=base)
assert small.delay == cfg.pacing.min_delay, "one recipient needs no spacing maths"
assert sch.build_plan(0, deadline, cfg.pacing, cfg.risk, now=base).recipients == 0
ok("degenerate counts (0 and 1 recipients) do not divide by zero")

# Risk bands escalate monotonically as the same window gets more people.
bands = [sch.build_plan(n, deadline, cfg.pacing, cfg.risk, now=base).risk
         for n in (100, 1400, 3000, 100000)]
assert bands == [sch.GREEN, sch.YELLOW, sch.RED, sch.IMPOSSIBLE], bands
ok(f"risk bands escalate with volume: {bands}")

huge = sch.build_plan(100000, deadline, cfg.pacing, cfg.risk, now=base)
assert huge.at_floor and not huge.feasible
assert huge.delay == cfg.pacing.min_delay, "must clamp at the floor, never below"
text = sch.describe(huge, cfg.risk)
assert "не укладываемся" in text and "⛔️" in text
ok("an impossible deadline is reported as impossible, not silently sped up")

past = sch.build_plan(50, base - timedelta(hours=1), cfg.pacing, cfg.risk, now=base)
assert past.seconds_available == 0 and past.risk == sch.IMPOSSIBLE
ok("a deadline already in the past is impossible, not negative")

for n in (0, 1, 100, 3000, 100000):
    sch.describe(sch.build_plan(n, deadline, cfg.pacing, cfg.risk, now=base), cfg.risk)
ok("describe() renders for every volume without crashing")

# --------------------------------------------------------------------------- #
# recipients and the years filter
# --------------------------------------------------------------------------- #

db = fresh_db()
for uid, age in ((1, 0.2), (2, 0.9), (3, 1.5), (4, 2.5), (5, 4.0)):
    db.upsert_recipient(person(uid, age))
db.upsert_recipient(Recipient(6, 60, None, "NoDate", None, None))
db.commit()

assert db.recipients_total() == 6
assert db.count_by_age(1) == 2, "within 1 year"
assert db.count_by_age(2) == 3
assert db.count_by_age(3) == 4
assert db.count_by_age(None) == 6, "'all' must include the dialogue with no date"
ok("years filter selects correctly; undated dialogues only appear under 'all'")

db.upsert_recipient(person(1, 0.1))
db.commit()
assert db.recipients_total() == 6, "re-scan must update, not duplicate"
ok("re-scanning dialogues updates rows instead of duplicating them")

# --------------------------------------------------------------------------- #
# campaigns: the count shown is the count queued
# --------------------------------------------------------------------------- #

shown = db.count_by_age(2)
cid = db.create_campaign("Привет!", None, 2.0, created_by=111, delay=30.0)
assert db.progress(cid)["total"] == shown, "queued count must match the panel's number"
ok(f"campaign freezes exactly the {shown} recipients the panel promised")

first = db.next_pending(cid)
assert first is not None and first.user_id == 1, "most recent dialogue goes first"
db.mark(cid, first.user_id, "sent")
assert db.progress(cid).get("sent") == 1
assert db.next_pending(cid).user_id != first.user_id
ok("queue drains in recency order and marks progress")

db.mark(cid, 2, "skipped", "UserPrivacyRestrictedError")
p = db.progress(cid)
assert p["sent"] == 1 and p["skipped"] == 1 and p.get("pending") == 1
assert db.sent_since(cid, time.time() - 60) == 1
assert db.sent_since(cid, time.time() + 60) == 0
ok("sent / skipped / pending are tracked separately")

# --------------------------------------------------------------------------- #
# the double-send guard
# --------------------------------------------------------------------------- #

assert db.count_by_age(2, exclude_sent=False) == shown
assert db.count_by_age(2) == shown - 1, "the person already sent to is excluded"
assert db.already_sent_in_range(2) == 1
cid2 = db.create_campaign("Ещё раз", None, 2.0, created_by=111, delay=30.0)
queued = [db.next_pending(cid2)]
assert 1 not in [r.user_id for r in queued if r], "must not re-invite an already-sent user"
ok("a new campaign never re-sends to someone who already received one")

# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #

db.finish_campaign(cid2, "stopped", "Остановлено вручную")
res = db.resumable_campaign()
assert res is not None and res["id"] == cid2 and res["pending_n"] > 0
db.reopen_campaign(cid2, 42.0)
assert db.get_campaign(cid2)["status"] == "running"
assert db.get_campaign(cid2)["delay"] == 42.0, "resume re-derives the pace"
assert db.active_campaign()["id"] == cid2

before = db.progress(cid2)
db.finish_campaign(cid2, "done", "")
while (r := db.next_pending(cid2)) is not None:
    db.mark(cid2, r.user_id, "sent")
assert db.resumable_campaign() is None, "nothing pending -> nothing to resume"
ok("stopped campaigns are resumable; drained ones are not")

# --------------------------------------------------------------------------- #
# panel helpers
# --------------------------------------------------------------------------- #

assert bot_mod.age_from_callback("age:2") == 2.0
assert bot_mod.age_from_callback("age:all") is None
ok("filter buttons decode to the right age window")

assert bot_mod.resolve_parse_mode("hi", "hi") == ("hi", None), "plain text stays plain"
assert bot_mod.resolve_parse_mode("<b>hi</b>", "hi") == ("<b>hi</b>", "html")
weird, mode = bot_mod.resolve_parse_mode("<xx>hi</xx>", "hi")
assert mode is None or weird == "<xx>hi</xx>"
ok("formatting is kept only when Telethon can actually parse it")

assert bot_mod.bar(0, 10).startswith("░") and bot_mod.bar(10, 10) == "█" * 16
assert len(bot_mod.bar(3, 7)) == 16 and len(bot_mod.bar(0, 0)) == 16
ok("progress bar is fixed width, including when the total is zero")

# --------------------------------------------------------------------------- #
# error classification
# --------------------------------------------------------------------------- #

from telethon import errors  # noqa: E402

from tgsender import sender as sender_mod  # noqa: E402

assert errors.PeerFloodError in sender_mod.ACCOUNT_ERRORS
assert errors.UserPrivacyRestrictedError in sender_mod.PERMANENT_ERRORS
assert errors.FloodWaitError not in sender_mod.PERMANENT_ERRORS, "must not burn a recipient"
assert not set(sender_mod.ACCOUNT_ERRORS) & set(sender_mod.PERMANENT_ERRORS)
ok("error classes are disjoint and correctly assigned")

print("\nALL TESTS PASSED")
