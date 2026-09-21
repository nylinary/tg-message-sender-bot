"""When will a campaign finish at the chosen pace, and does that beat the deadline?

The interval is set by the user (⚙️ Настройки). This module works out what that
means: messages per hour and per day, the finish time once nights are skipped,
whether that lands before the deadline, and — if not — how many people the
campaign will actually reach before sending stops.

All datetimes here are timezone-aware and in the user's timezone, because quiet
hours are local hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Pacing, Risk

GREEN, YELLOW, RED = "green", "yellow", "red"
BADGE = {GREEN: "🟢", YELLOW: "🟡", RED: "🔴"}


def is_quiet(hour: int, start: int, end: int) -> bool:
    """Quiet hours may wrap past midnight (23 -> 10). start == end disables them."""
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def quiet_hours_per_day(start: int, end: int) -> int:
    if start == end:
        return 0
    return (24 - start + end) if start > end else (end - start)


def _chunks(start: datetime, stop: datetime):
    """Walk [start, stop) in pieces that never cross an hour boundary."""
    cursor = start
    while cursor < stop:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        chunk_end = min(next_hour, stop)
        yield cursor, chunk_end
        cursor = chunk_end


def active_seconds(now: datetime, deadline: datetime, start: int, end: int) -> float:
    """Sendable seconds between two moments, excluding quiet hours."""
    if deadline <= now:
        return 0.0
    return sum(
        (b - a).total_seconds()
        for a, b in _chunks(now, deadline)
        if not is_quiet(a.hour, start, end)
    )


def advance(now: datetime, seconds: float, start: int, end: int) -> datetime:
    """The moment `seconds` of sending time have elapsed, skipping quiet hours."""
    remaining = seconds
    horizon = now + timedelta(days=3650)
    for a, b in _chunks(now, horizon):
        if is_quiet(a.hour, start, end):
            continue
        span = (b - a).total_seconds()
        if remaining <= span:
            return a + timedelta(seconds=remaining)
        remaining -= span
    return horizon


@dataclass(frozen=True)
class Estimate:
    recipients: int
    interval: float
    jitter: float
    now: datetime
    deadline: datetime
    seconds_available: float
    seconds_needed: float
    finishes_at: datetime
    capacity: int          # how many get it before the deadline stops sending
    per_hour: float
    per_day: float
    risk: str

    @property
    def fits(self) -> bool:
        return self.capacity >= self.recipients

    @property
    def missed(self) -> int:
        return max(0, self.recipients - self.capacity)


def estimate(
    recipients: int,
    interval: float,
    jitter: float,
    deadline: datetime,
    pacing: Pacing,
    risk_cfg: Risk,
    now: datetime,
    pause_at_night: bool = True,
) -> Estimate:
    # Sending silently at night means the night is ordinary sending time.
    qs, qe = (pacing.quiet_start, pacing.quiet_end) if pause_at_night else (0, 0)
    # Jitter is symmetric, so on average it cancels; the long breaks do not.
    long_break = (pacing.long_pause_min + pacing.long_pause_max) / 2
    per_message = interval + long_break / max(1, pacing.long_pause_every)

    gaps = max(0, recipients - 1)
    needed = gaps * interval + (gaps // max(1, pacing.long_pause_every)) * long_break
    available = active_seconds(now, deadline, qs, qe)

    if recipients <= 0 or available <= 0:
        capacity = 0 if available <= 0 else recipients
    else:
        # The first message goes out at once; each further one costs a gap.
        capacity = min(recipients, 1 + int(available // per_message))

    per_hour = 3600 / interval
    sendable_hours = 24 - quiet_hours_per_day(qs, qe)
    per_day = sendable_hours * 3600 / per_message

    if per_day >= risk_cfg.danger_per_day:
        band = RED
    elif per_day >= risk_cfg.warn_per_day:
        band = YELLOW
    else:
        band = GREEN

    return Estimate(
        recipients=recipients,
        interval=interval,
        jitter=jitter,
        now=now,
        deadline=deadline,
        seconds_available=available,
        seconds_needed=needed,
        finishes_at=advance(now, needed, qs, qe),
        capacity=capacity,
        per_hour=per_hour,
        per_day=per_day,
        risk=band,
    )


def humanize(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m} мин" + (f" {s} с" if s else "")
    if seconds < 86400:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"
    return f"{seconds // 86400} д {(seconds % 86400) // 3600} ч"


def describe(
    est: Estimate,
    pacing: Pacing,
    risk_cfg: Risk,
    timezone: str,
    pause_at_night: bool = True,
) -> str:
    """The pace and deadline block shown before launching."""
    night = "не шлём" if pause_at_night else "шлём без звука"
    lo, hi = est.interval * (1 - est.jitter), est.interval * (1 + est.jitter)
    lines = [
        f"👥 Получателей: <b>{est.recipients}</b>",
        f"⏱ Интервал: <b>{humanize(est.interval)}</b>"
        + (f" ±{est.jitter * 100:.0f}% <i>({humanize(lo)}–{humanize(hi)})</i>"
           if est.jitter else ""),
        f"📈 Темп: ~<b>{est.per_hour:.0f}/час</b>, ~<b>{est.per_day:.0f}/сутки</b>",
        f"🌙 Ночью {night}: {pacing.quiet_start:02d}:00–{pacing.quiet_end:02d}:00 "
        f"<i>({timezone})</i>",
        f"🏁 Дедлайн: <b>{est.deadline:%d.%m %H:%M}</b>",
    ]
    if est.recipients == 0:
        lines.append("\nПод фильтр никто не попал.")
        return "\n".join(lines)

    if est.seconds_available <= 0:
        lines += ["", "⛔️ <b>Дедлайн уже прошёл.</b> Поменяй его в ⚙️ Настройках."]
    elif est.fits:
        lines.append(f"✅ Успеваем: закончим ~<b>{est.finishes_at:%d.%m %H:%M}</b>")
    else:
        lines += [
            "",
            f"⛔️ <b>Не успеваем.</b> До дедлайна уйдёт ~<b>{est.capacity}</b> из "
            f"<b>{est.recipients}</b>, остальным ~{est.missed} — нет: в дедлайн "
            f"рассылка остановится.",
        ]
        needed = _interval_to_fit(est, pacing)
        if needed >= pacing.min_interval:
            lines.append(
                f"Чтобы успеть всем, нужен интервал ~<b>{humanize(needed)}</b> — "
                f"или сузь фильтр, или сдвинь дедлайн."
            )
        else:
            lines.append(
                "Даже на минимальном интервале всем не успеть — сузь фильтр "
                "или сдвинь дедлайн."
            )

    badge = BADGE[est.risk]
    if est.risk == RED:
        lines += [
            "",
            f"{badge} <b>Очень высокий темп</b> — выше {risk_cfg.danger_per_day}/сутки, "
            f"где аккаунты обычно получают ограничения. При PEER_FLOOD рассылка "
            f"остановится сама.",
        ]
    elif est.risk == YELLOW:
        lines += [
            "",
            f"{badge} Темп выше спокойного ({risk_cfg.warn_per_day}/сутки). "
            f"Обычно проходит на прогретом аккаунте.",
        ]
    else:
        lines.append(f"{badge} Темп спокойный.")
    return "\n".join(lines)


def _interval_to_fit(est: Estimate, pacing: Pacing) -> float:
    gaps = max(1, est.recipients - 1)
    long_break = (pacing.long_pause_min + pacing.long_pause_max) / 2
    breaks = (gaps // max(1, pacing.long_pause_every)) * long_break
    return max(0.0, (est.seconds_available - breaks) / gaps)
