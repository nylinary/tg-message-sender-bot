"""Ask @SpamBot whether the sending account is limited, on demand and on a timer.

What @SpamBot can and cannot tell you: it answers with the account's current
state only — no limits, limited until a date, or limited indefinitely. It does
not report how many complaints there were, and there is no "at risk" warning;
Telegram does not expose either. So this is a tripwire, not a forecast: its
value is catching a limit early and stopping the campaign before continued
sending turns a temporary limit into a permanent one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from . import settings as settings_mod

log = logging.getLogger("tgsender.spam")

SPAMBOT = "SpamBot"
OK, LIMITED, UNKNOWN, ERROR = "ok", "limited", "unknown", "error"
STORE_KEY = "spam_last"

_OK_MARKERS = (
    "no limits", "free as a bird", "никаких ограничений", "нет ограничений",
    "не наложено", "свободен от",
)
_LIMITED_MARKERS = (
    "limited", "limit", "harsh response", "annoying", "ограничен", "ограничение",
    "жалоб", "суров", "не сможете",
)
# "limited until 28 Sep 2026, 14:02 UTC", "ограничен до …", and the phrasing
# @SpamBot actually uses in Russian: "Ограничения будут автоматически сняты …".
_UNTIL = re.compile(
    r"(?:until|до|сняты|снято|released on|removed on|lifted on|lifted)\s+"
    r"(\d{1,2}\s+[^\s,.]+\.?\s+\d{4}(?:,?\s*(?:в\s*)?\d{1,2}:\d{2}(?:\s*UTC)?)?)",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTHS.update({m: i for i, m in enumerate(
    ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"), 1)})
_PARTS = re.compile(r"(\d{1,2})\s+([^\s,.]+)\.?\s+(\d{4})(?:,?\s*(?:в\s*)?(\d{1,2}):(\d{2}))?")


def until_timestamp(until: str | None) -> float | None:
    """'21 Sep 2026, 20:36 UTC' -> epoch seconds. @SpamBot quotes UTC."""
    if not until:
        return None
    m = _PARTS.search(until)
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower()[:3])
    if not month:
        return None
    try:
        moment = datetime(int(m.group(3)), month, int(m.group(1)),
                          int(m.group(4) or 0), int(m.group(5) or 0), tzinfo=timezone.utc)
    except ValueError:
        return None
    return moment.timestamp()


class SpamCheckError(Exception):
    pass


@dataclass
class SpamStatus:
    state: str
    until: str | None
    text: str
    checked_at: float
    until_ts: float | None = None

    @property
    def icon(self) -> str:
        return {OK: "✅", LIMITED: "🚨", UNKNOWN: "❓", ERROR: "⚠️"}[self.state]

    def headline(self, tz=None) -> str:
        if self.state == OK:
            return "ограничений нет"
        if self.state == LIMITED:
            if self.until_ts is not None:
                local = datetime.fromtimestamp(self.until_ts, tz or timezone.utc)
                return f"аккаунт ОГРАНИЧЕН до {local:%d.%m %H:%M}"
            if self.until:
                return f"аккаунт ОГРАНИЧЕН до {self.until}"
            return "аккаунт ОГРАНИЧЕН (срок не указан)"
        if self.state == UNKNOWN:
            return "ответ не удалось разобрать"
        return "проверка не удалась"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> SpamStatus | None:
        if not raw:
            return None
        try:
            return cls(**json.loads(raw))
        except (ValueError, TypeError):
            return None


def classify(text: str) -> tuple[str, str | None]:
    """Read @SpamBot's reply (English or Russian) into a state and end date."""
    low = text.lower()
    if any(m in low for m in _OK_MARKERS):
        return OK, None
    if any(m in low for m in _LIMITED_MARKERS):
        m = _UNTIL.search(text)
        return LIMITED, (m.group(1).strip() if m else None)
    return UNKNOWN, None


async def ask_spambot(client, timeout: float = 25.0, poll: float = 1.0) -> str:
    """Send /start to @SpamBot from the account and wait for its answer."""
    sent = await client.send_message(SPAMBOT, "/start")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        messages = await client.get_messages(SPAMBOT, limit=5)
        replies = [
            m for m in messages
            if m is not None and not m.out and m.id > sent.id and (m.message or "").strip()
        ]
        if replies:
            # It sometimes answers in two messages; give the second a moment.
            await asyncio.sleep(poll)
            messages = await client.get_messages(SPAMBOT, limit=5)
            replies = [
                m for m in messages
                if m is not None and not m.out and m.id > sent.id and (m.message or "").strip()
            ]
            return "\n\n".join(m.message for m in sorted(replies, key=lambda m: m.id))
    raise SpamCheckError(f"@SpamBot не ответил за {timeout:.0f} с")


class SpamWatch:
    """Runs checks, remembers the last result, and acts on a limit."""

    def __init__(self, cfg, db, client, worker, broadcast, *, ask=ask_spambot):
        self.cfg = cfg
        self.db = db
        self.client = client
        self.worker = worker
        self.broadcast = broadcast
        self._ask = ask
        self._lock = asyncio.Lock()

    async def last(self) -> SpamStatus | None:
        return SpamStatus.from_json((await self.db.get_settings()).get(STORE_KEY))

    async def check(self) -> tuple[SpamStatus, SpamStatus | None, bool]:
        """Returns (new status, previous status, whether a campaign was stopped)."""
        async with self._lock:
            previous = await self.last()
            try:
                text = await self._ask(self.client)
                state, until = classify(text)
            except Exception as exc:  # noqa: BLE001 - a failed check must be reported, not crash
                text, state, until = f"{type(exc).__name__}: {exc}", ERROR, None
            status = SpamStatus(state, until, text[:1500], time.time(), until_timestamp(until))
            await self.db.set_setting(STORE_KEY, status.to_json())
            log.info("SpamBot check: %s %s", state, until or "")

            stopped = False
            if state == LIMITED and self.worker.running:
                self.worker.request_stop(
                    "🚨 @SpamBot сообщил об ограничении аккаунта — рассылка остановлена "
                    "автоматически. Продолжать с ограниченного аккаунта опасно: временное "
                    "ограничение может стать постоянным."
                )
                stopped = True
            return status, previous, stopped

    async def report(self, status: SpamStatus, previous: SpamStatus | None,
                     stopped: bool) -> str:
        s = await settings_mod.load(self.db, self.cfg)
        when = datetime.fromtimestamp(status.checked_at, s.tz).strftime("%d.%m %H:%M")
        lines = [
            f"🛡 <b>@SpamBot: {status.headline(s.tz)}</b> {status.icon}",
            f"<i>Проверка {when}, время — {s.timezone}</i>",
        ]
        if status.state == LIMITED and status.until_ts is not None:
            left = status.until_ts - time.time()
            lines.append(
                f"Временное ограничение, снимется через ~{_hours(left)}."
                if left > 0 else
                "Срок ограничения уже истёк — нажми «Проверить» ещё раз через пару минут."
            )
        if previous and previous.state == LIMITED and status.state == OK:
            lines.append("\n🎉 Ограничения сняты.")
        if stopped:
            lines.append("\n⏹ <b>Рассылка остановлена автоматически.</b> Когда ограничение "
                         "снимут, её можно продолжить кнопкой «▶️ Продолжить».")

        cid = self.worker.campaign_id
        if cid is not None:
            progress = await self.db.progress(cid)
            since = previous.checked_at if previous else status.checked_at - 3600
            recent = await self.db.sent_since(cid, since)
            lines.append(
                f"\n📨 Рассылка #{cid}: отправлено {progress.get('sent', 0)} из "
                f"{progress.get('total', 0)}, с прошлой проверки — {recent}"
            )
            if self.worker.flood_waits:
                lines.append(f"⏳ FLOOD_WAIT от Telegram с начала рассылки: {self.worker.flood_waits}")

        if status.state != OK:
            lines.append(f"\n<blockquote>{_escape(status.text[:800])}</blockquote>")
        return "\n".join(lines)

    async def tick(self) -> float:
        """One scheduling step: check if due. Returns seconds until the next step."""
        s = await settings_mod.load(self.db, self.cfg)
        if s.spam_every_hours <= 0:
            return 300
        last = await self.last()
        wait = (last.checked_at if last else 0) + s.spam_every_hours * 3600 - time.time()
        if wait > 0:
            # Short naps, so a changed schedule is picked up within minutes.
            return min(wait, 300)
        status, previous, stopped = await self.check()
        if (
            s.spam_notify == settings_mod.SPAM_NOTIFY_ALWAYS
            or status.state != OK
            or (previous is not None and previous.state != OK)   # tell them it cleared
        ):
            await self.broadcast(await self.report(status, previous, stopped))
        return 300

    async def loop(self) -> None:
        await asyncio.sleep(30)  # let startup finish first
        while True:
            try:
                wait = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the watcher must never die
                log.exception("SpamBot watcher failed; retrying in 5 minutes")
                wait = 300
            await asyncio.sleep(wait)


def _hours(seconds: float) -> str:
    minutes = int(seconds // 60)
    return f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
