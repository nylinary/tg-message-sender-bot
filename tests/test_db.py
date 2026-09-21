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

ok = lambda m: print(f"  ok  {m}")
YEAR = 365.25 * 86400
TAG = uuid.uuid4().hex[:8]
ACCOUNT = f"test_{TAG}"
OTHER = f"other_{TAG}"


def person(uid: int, years_ago: float) -> Recipient:
    return Recipient(uid, uid * 10, f"u{uid}", f"N{uid}", None, time.time() - years_ago * YEAR)


async def main() -> None:
    db = await DB.connect(normalize_dsn(DSN), ACCOUNT)
    other = DB(db.pool, OTHER)

    try:
        # ---------------- recipients ----------------
        await db.upsert_recipients([person(i, a) for i, a in
                                    ((1, 0.2), (2, 0.9), (3, 1.5), (4, 2.5), (5, 4.0))])
        await db.upsert_recipients([Recipient(6, 60, None, "NoDate", None, None)])
        assert await db.recipients_total() == 6
        ok("batch upsert writes recipients")

        await db.upsert_recipients([person(1, 0.1), person(2, 0.8)])
        assert await db.recipients_total() == 6, "re-scan must update, not duplicate"
        ok("re-scanning updates rows instead of duplicating them")

        assert await db.count_by_age(1) == 2
        assert await db.count_by_age(2) == 3
        assert await db.count_by_age(3) == 4
        assert await db.count_by_age(None) == 6, "'all' includes the undated dialogue"
        ok("years filter selects correctly; undated dialogues only under 'all'")

        # ---------------- account isolation ----------------
        await other.upsert_recipients([person(1, 0.1), person(99, 0.1)])
        assert await other.recipients_total() == 2
        assert await db.recipients_total() == 6, "another account must not leak in"
        ok("two accounts sharing one database stay isolated")

        # ---------------- campaign freezes the promised list ----------------
        shown = await db.count_by_age(2)
        cid = await db.create_campaign("Привет!", None, 2.0, 111, 30.0)
        assert (await db.progress(cid))["total"] == shown
        ok(f"campaign queues exactly the {shown} recipients the panel promised")

        # ---------------- claim / in-flight ----------------
        first = await db.claim_next(cid)
        assert first is not None and first.user_id == 1, "most recent dialogue first"
        p = await db.progress(cid)
        assert p.get("sending") == 1
        assert p["pending"] == shown - 1 + 1, "in-flight still counts as outstanding"
        ok("claim_next marks in-flight; it still shows as outstanding")

        second = await db.claim_next(cid)
        assert second is not None and second.user_id != first.user_id, "no double hand-out"
        ok("a claimed recipient is never handed out twice")

        await db.mark(cid, first.user_id, "sent")
        await db.mark(cid, second.user_id, "skipped", "UserPrivacyRestrictedError")
        p = await db.progress(cid)
        assert p["sent"] == 1 and p["skipped"] == 1
        assert await db.sent_since(cid, time.time() - 60) == 1
        assert await db.sent_since(cid, time.time() + 60) == 0
        ok("sent / skipped are tracked separately and time-windowed")

        # ---------------- crash recovery ----------------
        stranded = await db.claim_next(cid)
        assert stranded is not None
        assert (await db.progress(cid)).get("sending") == 1
        freed = await db.release_inflight(cid)
        assert freed == 1, f"expected 1 requeued, got {freed}"
        assert (await db.progress(cid)).get("sending", 0) == 0
        assert await db.claim_next(cid) is not None, "requeued person is claimable again"
        await db.release_inflight(cid)
        ok("a crash mid-send requeues the in-flight recipient instead of losing them")

        # ---------------- double-send guard ----------------
        assert await db.count_by_age(2, exclude_sent=False) == shown
        assert await db.count_by_age(2) == shown - 1
        assert await db.already_sent_in_range(2) == 1
        cid2 = await db.create_campaign("Ещё раз", None, 2.0, 111, 30.0)
        got = []
        while (r := await db.claim_next(cid2)) is not None:
            got.append(r.user_id)
        assert first.user_id not in got, "must not re-invite an already-sent user"
        ok("a new campaign never re-sends to someone who already received one")

        # The guard must not reach across accounts.
        ocid = await other.create_campaign("x", None, None, 111, 30.0)
        assert (await other.progress(ocid))["total"] == 2, "other account unaffected"
        ok("the already-sent guard is scoped per account")

        # ---------------- resume ----------------
        await db.release_inflight(cid2)
        await db.finish_campaign(cid2, "stopped", "Остановлено вручную")
        res = await db.resumable_campaign()
        assert res is not None and res["id"] == cid2 and res["pending_n"] > 0
        await db.reopen_campaign(cid2, 42.0)
        row = await db.get_campaign(cid2)
        assert row["status"] == "running" and row["delay"] == 42.0
        assert (await db.active_campaign())["id"] == cid2
        ok("stopped campaigns resume and re-derive their pace")

        while (r := await db.claim_next(cid2)) is not None:
            await db.mark(cid2, r.user_id, "sent")
        await db.finish_campaign(cid2, "done", "")
        await db.finish_campaign(cid, "done", "")
        assert await db.resumable_campaign() is None, "drained -> nothing to resume"
        ok("a fully drained campaign is not offered for resume")

        errors = await db.recent_errors(cid)
        assert any(e["error"] for e in errors)
        ok("recent errors are readable for the status screen")

    finally:
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM deliveries WHERE campaign_id IN "
                "(SELECT id FROM campaigns WHERE account = ANY($1::text[]))",
                [ACCOUNT, OTHER],
            )
            await conn.execute(
                "DELETE FROM campaigns WHERE account = ANY($1::text[])", [ACCOUNT, OTHER]
            )
            await conn.execute(
                "DELETE FROM recipients WHERE account = ANY($1::text[])", [ACCOUNT, OTHER]
            )
        await db.close()


asyncio.run(main())
print("\nALL DATABASE TESTS PASSED")
