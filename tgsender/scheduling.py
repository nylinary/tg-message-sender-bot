from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import Pacing, Risk

GREEN, YELLOW, RED, IMPOSSIBLE = "green", "yellow", "red", "impossible"

BADGE = {
    GREEN: "🟢",
    YELLOW: "🟡",
    RED: "🔴",
    IMPOSSIBLE: "⛔️",
}


def is_quiet(hour: int, start: int, end: int) -> bool:
    """Quiet hours may wrap past midnight (23 -> 10). start == end disables them."""
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def active_seconds(now: datetime, deadline: datetime, start: int, end: int) -> float:
    """Sendable seconds between two moments, excluding quiet hours."""
    if deadline <= now:
        return 0.0

    total = 0.0
    cursor = now
    while cursor < deadline:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        chunk_end = min(next_hour, deadline)
        if not is_quiet(cursor.hour, start, end):
            total += (chunk_end - cursor).total_seconds()
        cursor = chunk_end
    return total


@dataclass(frozen=True)
class Plan:
    recipients: int
    now: datetime
    deadline: datetime
    seconds_available: float
    delay: float          # mean gap between messages, seconds
    per_hour: float       # while actually sending
    per_day: float        # spread over calendar days remaining
    risk: str
    at_floor: bool        # True when the deadline demands faster than min_delay

    @property
    def days_left(self) -> float:
        return max(0.0, (self.deadline - self.now).total_seconds() / 86400)

    @property
    def finishes_at(self) -> datetime:
        return self.now + timedelta(seconds=self.delay * max(0, self.recipients - 1))

    @property
    def feasible(self) -> bool:
        return self.risk != IMPOSSIBLE


def build_plan(
    recipients: int,
    deadline: datetime,
    pacing: Pacing,
    risk_cfg: Risk,
    now: datetime | None = None,
) -> Plan:
    """Work out the gap between messages needed to finish by the deadline."""
    now = now or datetime.now()
    available = active_seconds(now, deadline, pacing.quiet_start, pacing.quiet_end)

    if recipients <= 0:
        return Plan(0, now, deadline, available, pacing.min_delay, 0, 0, GREEN, False)

    # n messages need n-1 gaps. With a single recipient there is no gap to
    # stretch, so the window is irrelevant and the floor applies.
    if recipients == 1:
        band = GREEN if available > 0 else IMPOSSIBLE
        return Plan(
            recipients=1,
            now=now,
            deadline=deadline,
            seconds_available=available,
            delay=pacing.min_delay,
            per_hour=3600 / pacing.min_delay,
            per_day=1,
            risk=band,
            at_floor=False,
        )

    gaps = recipients - 1
    ideal = available / gaps if available > 0 else 0.0

    at_floor = ideal < pacing.min_delay
    delay = max(pacing.min_delay, ideal)

    per_hour = 3600 / delay
    days = max(available / 86400, (deadline - now).total_seconds() / 86400)
    per_day = recipients / days if days > 0 else float("inf")

    if available <= 0 or at_floor:
        # Even flat out we cannot make the deadline.
        band = IMPOSSIBLE
    elif per_day >= risk_cfg.danger_per_day:
        band = RED
    elif per_day >= risk_cfg.warn_per_day:
        band = YELLOW
    else:
        band = GREEN

    return Plan(
        recipients=recipients,
        now=now,
        deadline=deadline,
        seconds_available=available,
        delay=delay,
        per_hour=per_hour,
        per_day=per_day,
        risk=band,
        at_floor=at_floor,
    )


def humanize(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds} с"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    if seconds < 86400:
        return f"{seconds // 3600} ч {(seconds % 3600) // 60:02d} мин"
    return f"{seconds // 86400} д {(seconds % 86400) // 3600} ч"


def describe(plan: Plan, risk_cfg: Risk) -> str:
    """The block of text the panel shows before you press Send."""
    lines = [
        f"👥 Получателей: <b>{plan.recipients}</b>",
        f"⏳ До дедлайна: <b>{humanize((plan.deadline - plan.now).total_seconds())}</b>"
        f" (до {plan.deadline:%d.%m %H:%M})",
        f"🕐 Рабочего времени: <b>{humanize(plan.seconds_available)}</b>"
        f" <i>(без ночных часов)</i>",
    ]

    if plan.recipients == 0:
        lines.append("\nПод фильтр никто не попал.")
        return "\n".join(lines)

    lines += [
        "",
        f"📨 Интервал: <b>~{humanize(plan.delay)}</b> между сообщениями",
        f"📈 Темп: <b>~{plan.per_hour:.0f}/час</b>, <b>~{plan.per_day:.0f}/сутки</b>",
    ]

    badge = BADGE[plan.risk]
    if plan.risk == IMPOSSIBLE:
        overflow = math.ceil(plan.recipients - plan.seconds_available / plan.delay) - 1
        lines += [
            "",
            f"{badge} <b>В дедлайн не укладываемся.</b>",
            f"На максимальной скорости ({humanize(plan.delay)}/сообщение) успеем "
            f"отправить примерно <b>{int(plan.seconds_available / plan.delay)}</b> из "
            f"<b>{plan.recipients}</b> — не хватит на ~<b>{max(0, overflow)}</b>.",
            "",
            "Варианты: сузить фильтр, сдвинуть дедлайн в config.toml, "
            "или разослать остальным ссылку-приглашение через публичный бот.",
        ]
    elif plan.risk == RED:
        lines += [
            "",
            f"{badge} <b>Очень высокий темп.</b> {plan.per_day:.0f}/сутки — это выше "
            f"порога {risk_cfg.danger_per_day}/сутки, при котором аккаунты обычно "
            f"получают ограничение.",
            "Рассылка запустится, если подтвердишь. Следи за статусом: при PEER_FLOOD "
            "она остановится сама.",
        ]
    elif plan.risk == YELLOW:
        lines += [
            "",
            f"{badge} <b>Темп выше спокойного.</b> {plan.per_day:.0f}/сутки при "
            f"ориентире {risk_cfg.warn_per_day}/сутки. Рискованно, но обычно проходит "
            f"на прогретом аккаунте.",
        ]
    else:
        lines += ["", f"{badge} Темп в безопасном диапазоне."]

    return "\n".join(lines)
