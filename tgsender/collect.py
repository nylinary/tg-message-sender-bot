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


async def collect(client: TelegramClient, db: DB, progress=None) -> dict[str, int]:
    """Walk every dialogue on the account and cache the private ones."""
    counts = {"scanned": 0, "saved": 0, "skipped": 0}

    async for dialog in client.iter_dialogs():
        counts["scanned"] += 1

        entity = dialog.entity
        if skip_reason(entity) is not None:
            counts["skipped"] += 1
        else:
            db.upsert_recipient(
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

        if counts["scanned"] % 100 == 0:
            db.commit()
            if progress:
                await progress(counts)

    db.commit()
    if progress:
        await progress(counts)
    return counts
