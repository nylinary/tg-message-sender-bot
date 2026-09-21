from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipients (
    user_id         INTEGER PRIMARY KEY,
    access_hash     INTEGER,
    username        TEXT,
    first_name      TEXT,
    last_name       TEXT,
    last_message_at REAL,
    collected_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_last_message ON recipients(last_message_at);

CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    parse_mode  TEXT,
    max_age_yrs REAL,
    status      TEXT NOT NULL DEFAULT 'draft',
    created_by  INTEGER,
    created_at  REAL NOT NULL,
    started_at  REAL,
    finished_at REAL,
    stop_reason TEXT,
    delay       REAL
);

CREATE TABLE IF NOT EXISTS deliveries (
    campaign_id INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    error       TEXT,
    sent_at     REAL,
    PRIMARY KEY (campaign_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_deliv ON deliveries(campaign_id, status);
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


class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ #
    # recipients
    # ------------------------------------------------------------------ #

    def upsert_recipient(self, rec: Recipient) -> None:
        self.conn.execute(
            """
            INSERT INTO recipients
                (user_id, access_hash, username, first_name, last_name,
                 last_message_at, collected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                access_hash     = excluded.access_hash,
                username        = excluded.username,
                first_name      = excluded.first_name,
                last_name       = excluded.last_name,
                last_message_at = excluded.last_message_at,
                collected_at    = excluded.collected_at
            """,
            (
                rec.user_id,
                rec.access_hash,
                rec.username,
                rec.first_name,
                rec.last_name,
                rec.last_message_at,
                time.time(),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def recipients_total(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM recipients").fetchone()["n"]

    def last_collected_at(self) -> float | None:
        row = self.conn.execute(
            "SELECT MAX(collected_at) AS ts FROM recipients"
        ).fetchone()
        return row["ts"]

    def _selector(
        self, max_age_years: float | None, exclude_sent: bool
    ) -> tuple[str, list]:
        """WHERE clause picking recipients for a filter. Shared by count and insert,
        so the number shown in the panel is exactly the number queued."""
        clauses: list[str] = []
        params: list = []

        if max_age_years is not None:
            clauses.append("last_message_at IS NOT NULL AND last_message_at >= ?")
            params.append(time.time() - max_age_years * YEAR_SECONDS)

        if exclude_sent:
            # Nobody gets the invite twice because a run was restarted.
            clauses.append(
                "user_id NOT IN (SELECT user_id FROM deliveries WHERE status = 'sent')"
            )

        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def count_by_age(
        self, max_age_years: float | None, exclude_sent: bool = True
    ) -> int:
        where, params = self._selector(max_age_years, exclude_sent)
        return self.conn.execute(
            f"SELECT COUNT(*) AS n FROM recipients{where}", params
        ).fetchone()["n"]

    def already_sent_in_range(self, max_age_years: float | None) -> int:
        """How many under this filter already received an earlier campaign."""
        return self.count_by_age(max_age_years, exclude_sent=False) - self.count_by_age(
            max_age_years, exclude_sent=True
        )

    # ------------------------------------------------------------------ #
    # campaigns
    # ------------------------------------------------------------------ #

    def active_campaign(self) -> sqlite3.Row | None:
        marks = ",".join("?" * len(ACTIVE_STATUSES))
        return self.conn.execute(
            f"SELECT * FROM campaigns WHERE status IN ({marks}) ORDER BY id DESC LIMIT 1",
            ACTIVE_STATUSES,
        ).fetchone()

    def latest_campaign(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM campaigns ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def resumable_campaign(self) -> sqlite3.Row | None:
        """A stopped campaign that still has people waiting in it."""
        return self.conn.execute(
            """
            SELECT c.*, COUNT(d.user_id) AS pending_n
            FROM campaigns c
            JOIN deliveries d ON d.campaign_id = c.id AND d.status = 'pending'
            WHERE c.status IN ('stopped', 'failed')
            GROUP BY c.id
            ORDER BY c.id DESC
            LIMIT 1
            """
        ).fetchone()

    def reopen_campaign(self, campaign_id: int, delay: float) -> None:
        self.conn.execute(
            "UPDATE campaigns SET status = 'running', finished_at = NULL, "
            "stop_reason = NULL, delay = ? WHERE id = ?",
            (delay, campaign_id),
        )
        self.conn.commit()

    def get_campaign(self, campaign_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM campaigns WHERE id = ?", (campaign_id,)
        ).fetchone()

    def create_campaign(
        self,
        text: str,
        parse_mode: str | None,
        max_age_years: float | None,
        created_by: int,
        delay: float,
        exclude_sent: bool = True,
    ) -> int:
        """Create a campaign and freeze its recipient list in one transaction."""
        where, params = self._selector(max_age_years, exclude_sent)
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO campaigns
                    (text, parse_mode, max_age_yrs, status, created_by, created_at, delay)
                VALUES (?, ?, ?, 'running', ?, ?, ?)
                """,
                (text, parse_mode, max_age_years, created_by, time.time(), delay),
            )
            campaign_id = cur.lastrowid
            self.conn.execute(
                f"""
                INSERT INTO deliveries (campaign_id, user_id, status)
                SELECT ?, user_id, 'pending' FROM recipients{where}
                """,
                [campaign_id, *params],
            )
            self.conn.execute(
                "UPDATE campaigns SET started_at = ? WHERE id = ?",
                (time.time(), campaign_id),
            )
        return campaign_id

    def finish_campaign(self, campaign_id: int, status: str, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE campaigns SET status = ?, finished_at = ?, stop_reason = ? "
            "WHERE id = ?",
            (status, time.time(), reason[:500], campaign_id),
        )
        self.conn.commit()

    def set_campaign_delay(self, campaign_id: int, delay: float) -> None:
        self.conn.execute(
            "UPDATE campaigns SET delay = ? WHERE id = ?", (delay, campaign_id)
        )
        self.conn.commit()

    # ------------------------------------------------------------------ #
    # deliveries
    # ------------------------------------------------------------------ #

    def next_pending(self, campaign_id: int) -> Recipient | None:
        row = self.conn.execute(
            """
            SELECT r.* FROM deliveries d
            JOIN recipients r ON r.user_id = d.user_id
            WHERE d.campaign_id = ? AND d.status = 'pending'
            ORDER BY r.last_message_at DESC
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        if row is None:
            return None
        return Recipient(
            user_id=row["user_id"],
            access_hash=row["access_hash"],
            username=row["username"],
            first_name=row["first_name"],
            last_name=row["last_name"],
            last_message_at=row["last_message_at"],
        )

    def mark(
        self,
        campaign_id: int,
        user_id: int,
        status: str,
        error: str = "",
    ) -> None:
        self.conn.execute(
            "UPDATE deliveries SET status = ?, error = ?, sent_at = ? "
            "WHERE campaign_id = ? AND user_id = ?",
            (
                status,
                error[:500] or None,
                time.time() if status == "sent" else None,
                campaign_id,
                user_id,
            ),
        )
        self.conn.commit()

    def progress(self, campaign_id: int) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM deliveries WHERE campaign_id = ? "
            "GROUP BY status",
            (campaign_id,),
        ).fetchall()
        counts = {r["status"]: r["n"] for r in rows}
        counts["total"] = sum(counts.values())
        return counts

    def sent_since(self, campaign_id: int, since_ts: float) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM deliveries WHERE campaign_id = ? "
            "AND sent_at IS NOT NULL AND sent_at >= ?",
            (campaign_id, since_ts),
        ).fetchone()["n"]

    def recent_errors(self, campaign_id: int, limit: int = 5) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT user_id, status, error FROM deliveries WHERE campaign_id = ? "
            "AND error IS NOT NULL ORDER BY rowid DESC LIMIT ?",
            (campaign_id, limit),
        ).fetchall()
