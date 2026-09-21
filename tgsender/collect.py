from __future__ import annotations

from telethon import TelegramClient
from telethon.tl.types import User

from .db import DB, Recipient

SYSTEM_IDS = {777000}  # Telegram service notifications


def skip_reason(entity) -> str | None:
    if not isinstance(entity, User):
        return "not a private chat"
    if entity.is_self:
        return "saved messages"
    if entity.bot:
        return "bot"
    if entity.deleted:
        return "deleted account"
    if getattr(entity, "support", False):
        return "telegram support"
    if entity.id in SYSTEM_IDS:
        return "service account"
    return None


def dialog_timestamp(dialog) -> float | None:
    """When this conversation last saw any traffic."""
    for candidate in (getattr(dialog, "date", None), getattr(dialog.message, "date", None)):
        if candidate is not None:
            return candidate.timestamp()
    return None


BATCH_SIZE = 200


async def collect(client: TelegramClient, db: DB, progress=None) -> dict[str, int]:
    """Walk every dialogue on the account and cache the private ones."""
    counts = {"scanned": 0, "saved": 0, "skipped": 0}
    batch: list[Recipient] = []

    async for dialog in client.iter_dialogs():
        counts["scanned"] += 1

        entity = dialog.entity
        if skip_reason(entity) is not None:
            counts["skipped"] += 1
        else:
            batch.append(
                Recipient(
                    user_id=entity.id,
                    access_hash=entity.access_hash,
                    username=entity.username,
                    first_name=entity.first_name,
                    last_name=entity.last_name,
                    last_message_at=dialog_timestamp(dialog),
                )
            )
            counts["saved"] += 1

        # Write in batches: one round trip per dialogue would make a 4000-chat
        # scan painfully slow now that the database is across a network.
        if len(batch) >= BATCH_SIZE:
            await db.upsert_recipients(batch)
            batch.clear()
            if progress:
                await progress(counts)

    await db.upsert_recipients(batch)
    if progress:
        await progress(counts)
    return counts
