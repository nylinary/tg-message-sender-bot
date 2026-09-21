from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import asyncpg

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipients (
    account         TEXT   NOT NULL,
    user_id         BIGINT NOT NULL,
    access_hash     BIGINT,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    last_message_at DOUBLE PRECISION,
    collected_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (account, user_id)
);
CREATE INDEX IF NOT EXISTS idx_recipients_last
    ON recipients(account, last_message_at);

CREATE TABLE IF NOT EXISTS campaigns (
    id          BIGSERIAL PRIMARY KEY,
    account     TEXT NOT NULL,
    body        TEXT NOT NULL,
    parse_mode  TEXT,
    max_age_yrs DOUBLE PRECISION,
    status      TEXT NOT NULL DEFAULT 'draft',
    created_by  BIGINT,
    created_at  DOUBLE PRECISION NOT NULL,
    started_at  DOUBLE PRECISION,
    finished_at DOUBLE PRECISION,
    stop_reason TEXT,
    delay       DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_campaigns_account
    ON campaigns(account, status, id DESC);

CREATE TABLE IF NOT EXISTS deliveries (
    campaign_id BIGINT NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    user_id     BIGINT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    sent_at     DOUBLE PRECISION,
    PRIMARY KEY (campaign_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_queue
    ON deliveries(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_deliveries_sent
    ON deliveries(campaign_id, sent_at);
"""

ACTIVE_STATUSES = ("running", "paused")
YEAR_SECONDS = 365.25 * 86400


@dataclass
class Recipient:
    user_id: int
    access_hash: int | None
    username: str | None
    first_name: str | None
    last_name: str | None
    last_message_at: float | None

    @property
    def label(self) -> str:
        name = " ".join(
            p for p in ((self.first_name or "").strip(), (self.last_name or "").strip()) if p
        )
        return name or (f"@{self.username}" if self.username else str(self.user_id))


def _row_to_recipient(row: asyncpg.Record) -> Recipient:
    return Recipient(
        user_id=row["user_id"],
        access_hash=row["access_hash"],
        username=row["username"],
        first_name=row["first_name"],
        last_name=row["last_name"],
        last_message_at=row["last_message_at"],
    )


class DB:
    """Campaign state in Postgres.

    Every delivery is committed the moment it happens, so a redeploy, crash or
    restart loses at most the one message in flight — never the progress.
    """

    def __init__(self, pool: asyncpg.Pool, account: str):
        self.pool = pool
        self.account = account

    @classmethod
    async def connect(cls, dsn: str, account: str, *, min_size=1, max_size=5) -> DB:
        pool = await asyncpg.create_pool(
            dsn, min_size=min_size, max_size=max_size, command_timeout=30
        )
        async with pool.acquire() as conn:
            await conn.execute(SCHEMA)
        return cls(pool, account)

    async def close(self) -> None:
        await self.pool.close()

    # ------------------------------------------------------------------ #
    # recipients
    # ------------------------------------------------------------------ #

    async def upsert_recipients(self, batch: list[Recipient]) -> None:
        if not batch:
            return
        now = time.time()
        await self.pool.executemany(
            """
            INSERT INTO recipients
                (account, user_id, access_hash, username, first_name, last_name,
                 last_message_at, collected_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (account, user_id) DO UPDATE SET
                access_hash     = EXCLUDED.access_hash,
                username        = EXCLUDED.username,
                first_name      = EXCLUDED.first_name,
                last_name       = EXCLUDED.last_name,
                last_message_at = EXCLUDED.last_message_at,
                collected_at    = EXCLUDED.collected_at
            """,
            [
                (
                    self.account,
                    r.user_id,
                    r.access_hash,
                    r.username,
                    r.first_name,
                    r.last_name,
                    r.last_message_at,
                    now,
                )
                for r in batch
            ],
        )

    async def recipients_total(self) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM recipients WHERE account = $1", self.account
        )

    async def last_collected_at(self) -> float | None:
        return await self.pool.fetchval(
            "SELECT MAX(collected_at) FROM recipients WHERE account = $1", self.account
        )

    def _selector(
        self, max_age_years: float | None, exclude_sent: bool, start: int = 1
    ) -> tuple[str, list[Any]]:
        """WHERE clause picking recipients for a filter.

        Shared by the count and the insert so the number the panel promises is
        exactly the number that gets queued.
        """
        params: list[Any] = [self.account]
        clauses = [f"r.account = ${start}"]
        n = start

        if max_age_years is not None:
            n += 1
            clauses.append(
                f"r.last_message_at IS NOT NULL AND r.last_message_at >= ${n}"
            )
            params.append(time.time() - max_age_years * YEAR_SECONDS)

        if exclude_sent:
            # Nobody gets the invite twice because a run was restarted.
            n += 1
            clauses.append(
                f"""NOT EXISTS (
                    SELECT 1 FROM deliveries d
                    JOIN campaigns c ON c.id = d.campaign_id
                    WHERE d.user_id = r.user_id
                      AND d.status = 'sent'
                      AND c.account = ${n}
                )"""
            )
            params.append(self.account)

        return " WHERE " + " AND ".join(clauses), params

    async def count_by_age(
        self, max_age_years: float | None, exclude_sent: bool = True
    ) -> int:
        where, params = self._selector(max_age_years, exclude_sent)
        return await self.pool.fetchval(
            f"SELECT COUNT(*) FROM recipients r{where}", *params
        )

    async def already_sent_in_range(self, max_age_years: float | None) -> int:
        """How many under this filter already received an earlier campaign."""
        total = await self.count_by_age(max_age_years, exclude_sent=False)
        fresh = await self.count_by_age(max_age_years, exclude_sent=True)
        return total - fresh

    # ------------------------------------------------------------------ #
    # campaigns
    # ------------------------------------------------------------------ #

    async def active_campaign(self) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM campaigns WHERE account = $1 AND status = ANY($2::text[]) "
            "ORDER BY id DESC LIMIT 1",
            self.account,
            list(ACTIVE_STATUSES),
        )

    async def get_campaign(self, campaign_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM campaigns WHERE id = $1 AND account = $2",
            campaign_id,
            self.account,
        )

    async def resumable_campaign(self) -> asyncpg.Record | None:
        """A stopped campaign that still has people waiting in it."""
        return await self.pool.fetchrow(
            """
            SELECT c.*, COUNT(d.user_id) AS pending_n
            FROM campaigns c
            JOIN deliveries d ON d.campaign_id = c.id AND d.status = 'pending'
            WHERE c.account = $1 AND c.status IN ('stopped', 'failed')
            GROUP BY c.id
            ORDER BY c.id DESC
            LIMIT 1
            """,
            self.account,
        )

    async def create_campaign(
        self,
        body: str,
        parse_mode: str | None,
        max_age_years: float | None,
        created_by: int,
        delay: float,
        exclude_sent: bool = True,
    ) -> int:
        """Create a campaign and freeze its recipient list in one transaction."""
        async with self.pool.acquire() as conn, conn.transaction():
            campaign_id = await conn.fetchval(
                """
                INSERT INTO campaigns
                    (account, body, parse_mode, max_age_yrs, status, created_by,
                     created_at, started_at, delay)
                VALUES ($1, $2, $3, $4, 'running', $5, $6, $6, $7)
                RETURNING id
                """,
                self.account,
                body,
                parse_mode,
                max_age_years,
                created_by,
                time.time(),
                delay,
            )
            where, params = self._selector(max_age_years, exclude_sent, start=2)
            await conn.execute(
                f"""
                INSERT INTO deliveries (campaign_id, user_id, status)
                SELECT $1, r.user_id, 'pending' FROM recipients r{where}
                """,
                campaign_id,
                *params,
            )
        return campaign_id

    async def finish_campaign(
        self, campaign_id: int, status: str, reason: str = ""
    ) -> None:
        await self.pool.execute(
            "UPDATE campaigns SET status = $1, finished_at = $2, stop_reason = $3 "
            "WHERE id = $4",
            status,
            time.time(),
            reason[:500] or None,
            campaign_id,
        )

    async def reopen_campaign(self, campaign_id: int, delay: float) -> None:
        await self.pool.execute(
            "UPDATE campaigns SET status = 'running', finished_at = NULL, "
            "stop_reason = NULL, delay = $1 WHERE id = $2",
            delay,
            campaign_id,
        )

    async def set_campaign_delay(self, campaign_id: int, delay: float) -> None:
        await self.pool.execute(
            "UPDATE campaigns SET delay = $1 WHERE id = $2", delay, campaign_id
        )

    # ------------------------------------------------------------------ #
    # deliveries
    # ------------------------------------------------------------------ #

    async def claim_next(self, campaign_id: int) -> Recipient | None:
        """Take the next pending recipient and mark it in-flight, atomically.

        SKIP LOCKED means that if a second worker ever races this one (an
        overlapping deploy, say), the two cannot hand out the same person.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT d.user_id FROM deliveries d
                JOIN recipients r
                  ON r.user_id = d.user_id AND r.account = $2
                WHERE d.campaign_id = $1 AND d.status = 'pending'
                ORDER BY r.last_message_at DESC NULLS LAST
                FOR UPDATE OF d SKIP LOCKED
                LIMIT 1
                """,
                campaign_id,
                self.account,
            )
            if row is None:
                return None
            await conn.execute(
                "UPDATE deliveries SET status = 'sending' "
                "WHERE campaign_id = $1 AND user_id = $2",
                campaign_id,
                row["user_id"],
            )
            full = await conn.fetchrow(
                "SELECT * FROM recipients WHERE account = $1 AND user_id = $2",
                self.account,
                row["user_id"],
            )
        return _row_to_recipient(full) if full else None

    async def mark(
        self, campaign_id: int, user_id: int, status: str, error: str = ""
    ) -> None:
        await self.pool.execute(
            "UPDATE deliveries SET status = $1, error = $2, sent_at = $3 "
            "WHERE campaign_id = $4 AND user_id = $5",
            status,
            error[:500] or None,
            time.time() if status == "sent" else None,
            campaign_id,
            user_id,
        )

    async def release_inflight(self, campaign_id: int) -> int:
        """Return anything left 'sending' by a crash to the queue."""
        result = await self.pool.execute(
            "UPDATE deliveries SET status = 'pending' "
            "WHERE campaign_id = $1 AND status = 'sending'",
            campaign_id,
        )
        return int(result.split()[-1]) if result else 0

    async def progress(self, campaign_id: int) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT status, COUNT(*) AS n FROM deliveries WHERE campaign_id = $1 "
            "GROUP BY status",
            campaign_id,
        )
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        # A message in flight is still owed to someone; show it as outstanding.
        counts["pending"] = counts.get("pending", 0) + counts.get("sending", 0)
        return counts

    async def sent_since(self, campaign_id: int, since_ts: float) -> int:
        return await self.pool.fetchval(
            "SELECT COUNT(*) FROM deliveries WHERE campaign_id = $1 "
            "AND sent_at IS NOT NULL AND sent_at >= $2",
            campaign_id,
            since_ts,
        )

    async def recent_errors(self, campaign_id: int, limit: int = 5) -> list:
        return await self.pool.fetch(
            "SELECT user_id, status, error FROM deliveries WHERE campaign_id = $1 "
            "AND error IS NOT NULL ORDER BY sent_at DESC NULLS LAST, user_id DESC "
            "LIMIT $2",
            campaign_id,
            limit,
        )
