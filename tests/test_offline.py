"""Pure-logic tests. No database, no network:

    .venv/bin/python tests/test_offline.py
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("ADMIN_IDS", "111, 222")
os.environ["TG_ACCOUNT"] = "selftest"

from tgsender import bot as bot_mod  # noqa: E402
from tgsender import config as config_mod  # noqa: E402
from tgsender import scheduling as sch  # noqa: E402

ok = lambda m: print(f"  ok  {m}")
cfg = config_mod.load()

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

assert cfg.admin_ids == frozenset({111, 222}), "ADMIN_IDS must tolerate spaces"
assert config_mod.normalize_dsn("postgres://a:b@h/d") == "postgresql://a:b@h/d"
assert config_mod.normalize_dsn("postgresql+asyncpg://a@h/d") == "postgresql://a@h/d"
assert config_mod.normalize_dsn("postgresql://a@h/d") == "postgresql://a@h/d"
ok("config parses admins and normalises every Postgres URL scheme")

blank = config_mod.Config(**{**cfg.__dict__, "database_url": ""})
try:
    config_mod.require_database_url(blank)
except SystemExit as exc:
    assert "DATABASE_URL" in str(exc)
else:
    raise AssertionError("a missing DATABASE_URL must fail loudly, not silently")
ok("a missing DATABASE_URL is rejected with an actionable message")

# --------------------------------------------------------------------------- #
# quiet hours
# --------------------------------------------------------------------------- #

assert sch.is_quiet(2, 23, 10) and sch.is_quiet(23, 23, 10) and sch.is_quiet(9, 23, 10)
assert not sch.is_quiet(10, 23, 10) and not sch.is_quiet(15, 23, 10)
assert sch.is_quiet(3, 1, 6) and not sch.is_quiet(8, 1, 6)
assert not sch.is_quiet(5, 0, 0), "start == end disables quiet hours"
ok("quiet-hour window wraps past midnight and can be disabled")

base = datetime(2026, 9, 21, 12, 0)
assert sch.active_seconds(base, base + timedelta(hours=2), 23, 10) == 7200
assert sch.active_seconds(base, base, 23, 10) == 0
assert sch.active_seconds(base, base - timedelta(hours=5), 23, 10) == 0, "past deadline"
assert sch.active_seconds(base, base + timedelta(days=1), 23, 10) == 13 * 3600
assert sch.active_seconds(base.replace(minute=30), base + timedelta(hours=1), 23, 10) == 1800
assert sch.active_seconds(base, base + timedelta(days=1), 0, 0) == 86400
ok("active_seconds excludes quiet hours and handles partial hours")

# --------------------------------------------------------------------------- #
# deadline -> rate
# --------------------------------------------------------------------------- #

deadline = base + timedelta(days=4)
window = sch.active_seconds(base, deadline, 23, 10)

plan = sch.build_plan(100, deadline, cfg.pacing, cfg.risk, now=base)
assert abs(plan.delay - window / 99) < 1e-6, "delay must fill the window exactly"
assert plan.risk == sch.GREEN and plan.feasible and not plan.at_floor
assert plan.finishes_at <= deadline + timedelta(seconds=1), "must land inside the window"
ok(f"100 recipients over 4 days -> {sch.humanize(plan.delay)} apart, green")

one = sch.build_plan(1, deadline, cfg.pacing, cfg.risk, now=base)
assert one.delay == cfg.pacing.min_delay, "a single recipient has no gap to stretch"
assert sch.build_plan(0, deadline, cfg.pacing, cfg.risk, now=base).recipients == 0
ok("degenerate counts (0 and 1 recipients) do not divide by zero")

bands = [sch.build_plan(n, deadline, cfg.pacing, cfg.risk, now=base).risk
         for n in (100, 1400, 3000, 100000)]
assert bands == [sch.GREEN, sch.YELLOW, sch.RED, sch.IMPOSSIBLE], bands
ok(f"risk bands escalate with volume: {bands}")

huge = sch.build_plan(100000, deadline, cfg.pacing, cfg.risk, now=base)
assert huge.at_floor and not huge.feasible
assert huge.delay == cfg.pacing.min_delay, "must clamp at the floor, never below"
assert "не укладываемся" in sch.describe(huge, cfg.risk)
ok("an impossible deadline is reported as impossible, not silently sped up")

past = sch.build_plan(50, base - timedelta(hours=1), cfg.pacing, cfg.risk, now=base)
assert past.seconds_available == 0 and past.risk == sch.IMPOSSIBLE
ok("a deadline already in the past is impossible, not negative")

for n in (0, 1, 100, 3000, 100000):
    sch.describe(sch.build_plan(n, deadline, cfg.pacing, cfg.risk, now=base), cfg.risk)
ok("describe() renders for every volume without crashing")

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

print("\nALL OFFLINE TESTS PASSED")
