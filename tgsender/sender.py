from __future__ import annotations

import asyncio
import random
import time
from datetime import timedelta

from telethon import TelegramClient, errors
from telethon.tl.types import InputPeerUser

from . import settings as settings_mod
from .config import Config
from .db import DB
from .scheduling import is_quiet

# The recipient is unreachable and always will be — skip, keep going.
PERMANENT_ERRORS = (
    errors.UserPrivacyRestrictedError,
    errors.UserIsBlockedError,
    errors.InputUserDeactivatedError,
    errors.PeerIdInvalidError,
    errors.UserIdInvalidError,
    errors.ChatWriteForbiddenError,
)

# Something is wrong with *our* account — pushing on makes it permanent.
ACCOUNT_ERRORS = (
    errors.PeerFloodError,
    errors.UserDeactivatedBanError,
    errors.AuthKeyUnregisteredError,
    errors.SessionRevokedError,
    errors.SessionExpiredError,
)

MAX_FLOOD_WAIT = 6 * 3600


class SendWorker:
    """Owns the one campaign that may be in flight at a time."""

    def __init__(self, cfg: Config, db: DB, client: TelegramClient, on_event=None):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.on_event = on_event or self._noop
        self.task: asyncio.Task | None = None
        self.campaign_id: int | None = None
        self.multiplier = 1.0
        self.waiting_until: float | None = None
        self.wait_reason = ""
        self._stop = asyncio.Event()

    @staticmethod
    async def _noop(*_args, **_kwargs) -> None:
        return None

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, campaign_id: int) -> None:
        if self.running:
            raise RuntimeError("A campaign is already running")
        self.campaign_id = campaign_id
        self.multiplier = 1.0
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(self._run(campaign_id))

    def request_stop(self) -> None:
        self._stop.set()

    async def _sleep(self, seconds: float, reason: str) -> bool:
        """Sleep, but wake early if someone pressed Stop. False => stop."""
        seconds = max(0.0, seconds)
        self.waiting_until = time.time() + seconds
        self.wait_reason = reason
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return False
        except asyncio.TimeoutError:
            return True
        finally:
            self.waiting_until = None
            self.wait_reason = ""

    # ------------------------------------------------------------------ #
    # the loop
    # ------------------------------------------------------------------ #

    async def _run(self, campaign_id: int) -> None:
        p = self.cfg.pacing
        campaign = await self.db.get_campaign(campaign_id)
        text = campaign["body"]
        parse_mode = campaign["parse_mode"]
        sent_in_run = 0
        recipient = None
        stopped = "Остановлено вручную"

        try:
            while True:
                if self._stop.is_set():
                    return await self._finish(campaign_id, "stopped", stopped)

                # Settings are re-read every message, so changing the interval,
                # jitter, timezone or deadline in the panel applies right away.
                s = await settings_mod.load(self.db, self.cfg)
                if s.now() >= s.deadline:
                    return await self._deadline_reached(campaign_id, s)

                # Pace first, claim second: a claimed recipient is marked
                # in-flight, and holding one across a long sleep would strand
                # them if the process died mid-wait.
                if s.pause_at_night and not await self._wait_out_quiet_hours(s):
                    return await self._finish(campaign_id, "stopped", stopped)

                if sent_in_run and sent_in_run % p.long_pause_every == 0:
                    pause = random.uniform(p.long_pause_min, p.long_pause_max)
                    if not await self._sleep(pause, "длинная пауза"):
                        return await self._finish(campaign_id, "stopped", stopped)

                if sent_in_run:
                    gap = s.interval * self.multiplier
                    gap *= random.uniform(1 - s.jitter, 1 + s.jitter)
                    if not await self._sleep(gap, "интервал"):
                        return await self._finish(campaign_id, "stopped", stopped)

                # The wait may have carried us past the deadline or into the night.
                s = await settings_mod.load(self.db, self.cfg)
                if s.now() >= s.deadline:
                    return await self._deadline_reached(campaign_id, s)
                night = is_quiet(s.now().hour, p.quiet_start, p.quiet_end)
                if night and s.pause_at_night:
                    continue

                recipient = await self.db.claim_next(campaign_id)
                if recipient is None:
                    return await self._finish(campaign_id, "done", "")

                # At night the message still goes out, but without sound or
                # vibration on the recipient's phone.
                outcome = await self._send_one(
                    campaign_id, recipient, text, parse_mode, silent=night
                )
                recipient = None
                if outcome == "hard_stop":
                    return
                if outcome == "sent":
                    sent_in_run += 1
                    self.multiplier = max(1.0, self.multiplier * 0.97)

        except asyncio.CancelledError:
            if recipient is not None:
                await self.db.mark(campaign_id, recipient.user_id, "pending")
            await self.db.finish_campaign(campaign_id, "stopped", "Процесс остановлен")
            raise
        except Exception as exc:  # noqa: BLE001 - worker must not die silently
            if recipient is not None:
                await self.db.mark(campaign_id, recipient.user_id, "pending")
            await self._finish(campaign_id, "failed", f"{type(exc).__name__}: {exc}")

    async def _deadline_reached(self, campaign_id: int, s) -> None:
        left = (await self.db.progress(campaign_id)).get("pending", 0)
        await self._finish(
            campaign_id,
            "stopped",
            f"Дедлайн {s.deadline:%d.%m %H:%M} наступил — рассылка остановлена. "
            f"Не отправлено: {left}. Если нужно дослать, сдвинь дедлайн в "
            f"⚙️ Настройках и нажми «Продолжить».",
        )

    async def _send_one(
        self, campaign_id, recipient, text, parse_mode, *, silent: bool = False
    ) -> str:
        peer = (
            InputPeerUser(recipient.user_id, recipient.access_hash)
            if recipient.access_hash is not None
            else recipient.user_id
        )
        try:
            await self.client.send_message(
                peer, text, parse_mode=parse_mode, link_preview=True, silent=silent
            )
        except errors.FloodWaitError as exc:
            # Not this recipient's fault — put them back in the queue.
            await self.db.mark(campaign_id, recipient.user_id, "pending")
            self.multiplier = min(
                self.cfg.pacing.flood_backoff_max,
                self.multiplier * self.cfg.pacing.flood_backoff,
            )
            if exc.seconds > MAX_FLOOD_WAIT:
                await self._finish(
                    campaign_id,
                    "stopped",
                    f"FLOOD_WAIT {exc.seconds // 3600} ч — аккаунт ограничен. "
                    f"Продолжать нельзя, проверь @SpamBot.",
                )
                return "hard_stop"
            await self.on_event(
                "flood", seconds=exc.seconds, multiplier=self.multiplier
            )
            # Resuming on the exact second the wait expires is itself a bot tell.
            if not await self._sleep(exc.seconds + random.uniform(10, 60), "FLOOD_WAIT"):
                await self._finish(campaign_id, "stopped", "Остановлено вручную")
                return "hard_stop"
            return "retry"
        except ACCOUNT_ERRORS as exc:
            await self.db.mark(campaign_id, recipient.user_id, "pending")
            await self._finish(
                campaign_id,
                "stopped",
                f"{type(exc).__name__} — аккаунт ограничен или разлогинен. "
                f"Рассылка остановлена, чтобы не сделать хуже. Проверь @SpamBot.",
            )
            return "hard_stop"
        except PERMANENT_ERRORS as exc:
            await self.db.mark(
                campaign_id, recipient.user_id, "skipped", type(exc).__name__
            )
            return "skipped"
        except Exception as exc:  # noqa: BLE001 - one bad peer must not kill the run
            await self.db.mark(
                campaign_id, recipient.user_id, "failed", f"{type(exc).__name__}: {exc}"
            )
            return "failed"

        await self.db.mark(campaign_id, recipient.user_id, "sent")
        return "sent"

    async def _wait_out_quiet_hours(self, s) -> bool:
        p = self.cfg.pacing
        while True:
            now = s.now()
            if not is_quiet(now.hour, p.quiet_start, p.quiet_end):
                return True
            resume = now.replace(hour=p.quiet_end, minute=0, second=0, microsecond=0)
            if resume <= now:
                resume += timedelta(days=1)
            wait = (resume - now).total_seconds() + random.uniform(0, 600)
            if not await self._sleep(wait, f"ночная пауза до {p.quiet_end:02d}:00"):
                return False

    async def _finish(self, campaign_id: int, status: str, reason: str) -> None:
        await self.db.release_inflight(campaign_id)
        await self.db.finish_campaign(campaign_id, status, reason)
        await self.on_event(
            "finished", status=status, reason=reason, campaign_id=campaign_id
        )

    # ------------------------------------------------------------------ #
    # status
    # ------------------------------------------------------------------ #

    async def snapshot(self) -> dict:
        if self.campaign_id is None:
            return {"running": False}
        counts = await self.db.progress(self.campaign_id)
        campaign = await self.db.get_campaign(self.campaign_id)
        last_hour = await self.db.sent_since(self.campaign_id, time.time() - 3600)
        s = await settings_mod.load(self.db, self.cfg)
        return {
            "running": self.running,
            "campaign_id": self.campaign_id,
            "status": campaign["status"] if campaign else "unknown",
            "stop_reason": campaign["stop_reason"] if campaign else None,
            "counts": counts,
            "interval": s.interval,
            "jitter": s.jitter,
            "filter_json": campaign["filter_json"] if campaign else None,
            "multiplier": self.multiplier,
            "last_hour": last_hour,
            "waiting_for": (
                max(0, self.waiting_until - time.time()) if self.waiting_until else 0
            ),
            "wait_reason": self.wait_reason,
        }
