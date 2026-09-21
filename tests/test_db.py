"""Postgres layer tests. Needs a real database:

    DATABASE_URL=postgresql://... .venv/bin/python tests/test_db.py

Everything is written under throwaway account scopes and cleaned up at the end.
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

from tgsender.config import normalize_dsn  # noqa: E402
from tgsender.db import DB, Recipient  # noqa: E402
from tgsender.filters import DAY, Filter  # noqa: E402

ok = lambda m: print(f"  ok  {m}")
YEAR = 365.25 * DAY
TAG = uuid.uuid4().hex[:8]
ACCOUNT = f"test_{TAG}"
OTHER = f"other_{TAG}"


def person(uid, years_ago, first=None, last=None):
    return Recipient(uid, uid * 10, f"u{uid}", first or f"N{uid}", last,
                     time.time() - years_ago * YEAR)


def within(years):
    return Filter().with_period(time.time() - years * YEAR, None, f"до {years} г")


async def main() -> None:
    db = await DB.connect(normalize_dsn(DSN), ACCOUNT)
    other = DB(db.pool, OTHER)

    try:
        # ---------------- recipients + gender ----------------
        await db.upsert_recipients([
            person(1, 0.2, "Анна"), person(2, 0.9, "Никита", "Сысоев"),
            person(3, 1.5, "Саша"), person(4, 2.5, "Мария"), person(5, 4.0, "Олег"),
        ])
        await db.upsert_recipients([Recipient(6, 60, None, "Женя", "Петрова", None)])
        assert await db.recipients_total() == 6
        rows = {r["user_id"]: r["gender"] for r in await db.pool.fetch(
            "SELECT user_id, gender FROM recipients WHERE account=$1", ACCOUNT)}
        assert rows == {1: "f", 2: "m", 3: "u", 4: "f", 5: "m", 6: "f"}, rows
        ok("upsert guesses gender on the way in")

        await db.pool.execute("UPDATE recipients SET gender=NULL WHERE account=$1", ACCOUNT)
        assert await db.backfill_gender() == 6
        assert await db.backfill_gender() == 0, "second backfill has nothing to do"
        assert await db.pool.fetchval(
            "SELECT COUNT(*) FROM recipients WHERE account=$1 AND gender IS NULL", ACCOUNT
        ) == 0
        ok("backfill fills gender for rows collected before the column existed")

        # ---------------- periods ----------------
        assert await db.count(within(1)) == 2
        assert await db.count(within(2)) == 3
        assert await db.count(within(3)) == 4
        assert await db.count(Filter()) == 6, "'все' includes the undated dialogue"
        older = Filter().with_period(time.time() - 3 * YEAR, time.time() - 1 * YEAR, "1-3")
        assert await db.count(older) == 2, "a range excludes both ends' outsiders"
        only_old = Filter().with_period(None, time.time() - 2 * YEAR, "давнее 2 г")
        assert await db.count(only_old) == 2, "open lower bound, undated excluded"
        ok("period filters: presets, ranges, open-ended, undated only under 'все'")

        # ---------------- gender ----------------
        women = Filter(genders=frozenset({"f"}))
        assert await db.count(women) == 3
        assert await db.count(Filter(genders=frozenset({"m"}))) == 2
        assert await db.count(Filter(genders=frozenset({"u"}))) == 1
        assert await db.count(Filter(genders=frozenset({"f", "u"}))) == 4
        assert await db.count(within(2).toggle("m").toggle("u")) == 1
        ok("gender filters, alone and combined with a period")

        by = await db.breakdown(within(3).toggle("m"))
        assert by == {"f": 2, "m": 1, "u": 1}, "breakdown ignores the gender toggle on purpose"
        sample = await db.sample(women, 10)
        assert {r.user_id for r in sample} == {1, 4, 6} and all(r.gender == "f" for r in sample)
        ok("breakdown shows all three groups; sample respects the filter")

        # ---------------- isolation ----------------
        await other.upsert_recipients([person(1, 0.1, "Олег"), person(99, 0.1)])
        assert await other.recipients_total() == 2 and await db.recipients_total() == 6
        ok("two accounts sharing one database stay isolated")

        # ---------------- settings ----------------
        assert await db.get_settings() == {}
        await db.set_setting("interval", "120", 111)
        await db.set_setting("interval", "90", 222)
        await db.set_setting("timezone", "Asia/Almaty")
        assert await db.get_settings() == {"interval": "90", "timezone": "Asia/Almaty"}
        assert await other.get_settings() == {}, "settings are per account"
        ok("settings: upsert, last write wins, scoped per account")

        # ---------------- campaign freezes the promised list ----------------
        flt = within(2).toggle("m")
        shown = await db.count(flt)
        cid = await db.create_campaign("Привет!", None, flt, 111, 180.0)
        assert (await db.progress(cid))["total"] == shown == 2
        row = await db.get_campaign(cid)
        assert Filter.from_json(row["filter_json"]) == flt, "filter stored with the campaign"
        ok("campaign queues exactly what the panel counted, and remembers its filter")

        cid = await db.create_campaign("Привет!", None, within(2), 111, 180.0)
        first = await db.claim_next(cid)
        assert first.user_id == 1 and first.gender == "f", "most recent first, gender loaded"
        second = await db.claim_next(cid)
        assert second.user_id != first.user_id
        await db.mark(cid, first.user_id, "sent")
        await db.mark(cid, second.user_id, "skipped", "UserPrivacyRestrictedError")
        p = await db.progress(cid)
        assert p["sent"] == 1 and p["skipped"] == 1
        ok("claim / mark / progress")

        stranded = await db.claim_next(cid)
        assert await db.release_inflight(cid) == 1
        assert (await db.claim_next(cid)).user_id == stranded.user_id
        await db.release_inflight(cid)
        ok("a crash mid-send requeues the in-flight recipient")

        # ---------------- double-send guard ----------------
        assert await db.count(within(2)) == 2 and await db.already_sent_in_range(within(2)) == 1
        cid2 = await db.create_campaign("Ещё", None, within(2), 111, 180.0)
        got = []
        while (r := await db.claim_next(cid2)) is not None:
            got.append(r.user_id)
        assert first.user_id not in got
        by = await db.breakdown(within(2))
        assert sum(by.values()) == 2, "breakdown excludes people already invited"
        ok("nobody is invited twice; counts agree")

        # ---------------- resume ----------------
        await db.release_inflight(cid2)
        await db.finish_campaign(cid2, "stopped", "manual")
        await db.finish_campaign(cid, "stopped", "manual")
        res = await db.resumable_campaign()
        assert res is not None and res["pending_n"] > 0
        await db.reopen_campaign(res["id"], 60.0)
        assert (await db.get_campaign(res["id"]))["status"] == "running"
        ok("stopped campaigns resume")

    finally:
        async with db.pool.acquire() as conn:
            scopes = [ACCOUNT, OTHER]
            await conn.execute(
                "DELETE FROM deliveries WHERE campaign_id IN "
                "(SELECT id FROM campaigns WHERE account = ANY($1::text[]))", scopes)
            await conn.execute("DELETE FROM campaigns WHERE account = ANY($1::text[])", scopes)
            await conn.execute("DELETE FROM recipients WHERE account = ANY($1::text[])", scopes)
            await conn.execute("DELETE FROM settings WHERE account = ANY($1::text[])", scopes)
        await db.close()


asyncio.run(main())
print("\nALL DATABASE TESTS PASSED")
