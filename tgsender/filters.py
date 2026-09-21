"""Who a campaign goes to: a period of last contact plus a set of genders.

A Filter is the single description shared by "🔎 Посчитать" and "📝 Новая
рассылка", so the number the panel shows is built from exactly the same
object that later freezes the recipient list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from .gender import FEMALE, ICONS, MALE, UNKNOWN

ALL_GENDERS = frozenset({FEMALE, MALE, UNKNOWN})
DAY = 86400.0

# (key, button label, description, max age in days — None means everything)
PRESETS: list[tuple[str, str, str, float | None]] = [
    ("3m", "3 мес", "до 3 месяцев", 91),
    ("6m", "6 мес", "до 6 месяцев", 182),
    ("1y", "1 год", "до 1 года", 365),
    ("2y", "2 года", "до 2 лет", 730),
    ("3y", "3 года", "до 3 лет", 1096),
    ("all", "Все", "все диалоги", None),
]


class PeriodError(ValueError):
    """Raised with a message that can be shown to the user as-is."""


@dataclass(frozen=True)
class Filter:
    since_ts: float | None = None   # last message no earlier than this
    until_ts: float | None = None   # last message no later than this
    genders: frozenset[str] = field(default_factory=lambda: ALL_GENDERS)
    label: str = "все диалоги"

    @property
    def dated(self) -> bool:
        """Any date bound excludes dialogues whose date Telegram did not give us."""
        return self.since_ts is not None or self.until_ts is not None

    @property
    def all_genders(self) -> bool:
        return self.genders >= ALL_GENDERS

    def with_period(self, since_ts, until_ts, label) -> Filter:
        return replace(self, since_ts=since_ts, until_ts=until_ts, label=label)

    def toggle(self, gender: str) -> Filter:
        """Flip one gender, refusing to leave the selection empty."""
        new = set(self.genders) ^ {gender}
        return replace(self, genders=frozenset(new or self.genders))

    def genders_text(self) -> str:
        if self.all_genders:
            return "все"
        return " + ".join(
            {FEMALE: "♀ женщины", MALE: "♂ мужчины", UNKNOWN: "❔ не определён"}[g]
            for g in (FEMALE, MALE, UNKNOWN)
            if g in self.genders
        )

    def summary(self) -> str:
        return f"{self.label} · {self.genders_text()}"

    def to_json(self) -> str:
        return json.dumps(
            {
                "since_ts": self.since_ts,
                "until_ts": self.until_ts,
                "genders": sorted(self.genders),
                "label": self.label,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> Filter:
        if not raw:
            return cls()
        data = json.loads(raw)
        return cls(
            since_ts=data.get("since_ts"),
            until_ts=data.get("until_ts"),
            genders=frozenset(data.get("genders") or ALL_GENDERS),
            label=data.get("label") or "все диалоги",
        )


def preset(key: str, now: datetime) -> tuple[float | None, float | None, str]:
    for k, _button, description, days in PRESETS:
        if k == key:
            since = None if days is None else now.timestamp() - days * DAY
            return since, None, description
    raise PeriodError(f"Неизвестный период: {key}")


def gender_button(f: Filter, gender: str) -> str:
    mark = "✅" if gender in f.genders else "▫️"
    name = {FEMALE: "Женщины", MALE: "Мужчины", UNKNOWN: "Не опр."}[gender]
    return f"{ICONS[gender]} {name} {mark}"


# --------------------------------------------------------------------------- #
# typed periods: "30д-1г", "7-90", "3м", "01.03.2025-01.09.2026", "01.03.2025"
# --------------------------------------------------------------------------- #

_UNITS: list[tuple[tuple[str, ...], float, str]] = [
    (("д", "дн", "дня", "день", "дней", "d", "day", "days"), 1, "дн"),
    (("н", "нед", "неделя", "недели", "недель", "w", "week", "weeks"), 7, "нед"),
    (("м", "мес", "месяц", "месяца", "месяцев", "m", "mo", "month", "months"), 30.44, "мес"),
    (("г", "л", "год", "года", "лет", "y", "year", "years"), 365.25, "г"),
]
_REL = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([a-zа-яё]*)$")
_DATE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$")


def _relative_days(token: str) -> tuple[float, str]:
    m = _REL.match(token)
    if not m:
        raise PeriodError(f"Не понял «{token}». Пример: 30д, 6м, 2г.")
    value = float(m.group(1).replace(",", "."))
    unit = m.group(2) or "д"
    for names, days, short in _UNITS:
        if unit in names:
            shown = f"{m.group(1)} {short}"
            return value * days, shown
    raise PeriodError(f"Неизвестная единица «{unit}». Используй д, н, м или г.")


def _date(token: str, now: datetime, end_of_day: bool) -> datetime:
    m = _DATE.match(token)
    if not m:
        raise PeriodError(f"Не понял дату «{token}». Формат: 01.03.2025")
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else now.year
    if year < 100:
        year += 2000
    try:
        moment = now.replace(year=year, month=month, day=day,
                             hour=0, minute=0, second=0, microsecond=0)
    except ValueError as exc:
        raise PeriodError(f"Такой даты нет: «{token}».") from exc
    return moment + timedelta(days=1) - timedelta(seconds=1) if end_of_day else moment


def parse_period(text: str, now: datetime) -> tuple[float | None, float | None, str]:
    """Turn what the user typed into (since_ts, until_ts, label)."""
    raw = text.strip().lower().replace("ё", "е")
    raw = re.sub(r"\b(от|с|назад|ago|from)\b", " ", raw)
    raw = re.sub(r"\s*(—|–|\bдо\b|\bпо\b|\bto\b)\s*", "-", raw)
    raw = re.sub(r"\s+", " ", raw).strip(" -") if raw.count("-") == 0 else raw.strip()
    if not raw:
        raise PeriodError("Пустой период.")

    parts = [p.strip() for p in raw.split("-")]
    if len(parts) > 2:
        raise PeriodError("Нужен один диапазон: «от-до».")
    if len(parts) == 1:
        # A single value means "from then until now".
        only = parts[0]
        if _DATE.match(only):
            since = _date(only, now, end_of_day=False)
            return since.timestamp(), None, f"с {since:%d.%m.%Y}"
        days, shown = _relative_days(only)
        return now.timestamp() - days * DAY, None, f"до {shown}"

    left, right = parts
    if _DATE.match(left) or _DATE.match(right):
        since = _date(left, now, end_of_day=False) if left else None
        until = _date(right, now, end_of_day=True) if right else None
        if since and until and since > until:
            since, until = _date(right, now, False), _date(left, now, True)
        label = (
            f"{since:%d.%m.%Y}" if since else "…"
        ) + " — " + (f"{until:%d.%m.%Y}" if until else "сегодня")
        return (
            since.timestamp() if since else None,
            until.timestamp() if until else None,
            label,
        )

    # Relative range: "30д-1г" = last spoke between 30 days and 1 year ago.
    near_days, near_shown = _relative_days(left) if left else (0.0, "0")
    far_days, far_shown = _relative_days(right) if right else (None, "")
    if far_days is not None and near_days > far_days:
        near_days, far_days = far_days, near_days
        near_shown, far_shown = far_shown, near_shown
    since = None if far_days is None else now.timestamp() - far_days * DAY
    until = None if near_days == 0 else now.timestamp() - near_days * DAY
    if far_days is None:
        label = f"давнее {near_shown}"
    elif near_days == 0:
        label = f"до {far_shown}"
    else:
        label = f"от {near_shown} до {far_shown}"
    return since, until, label


PERIOD_HELP = (
    "✏️ <b>Напиши период последнего диалога.</b> Примеры:\n\n"
    "• <code>2г</code> — за последние 2 года\n"
    "• <code>3м</code> — за последние 3 месяца\n"
    "• <code>30д-1г</code> — от 30 дней до года назад\n"
    "• <code>7-90</code> — от 7 до 90 дней (без единиц — дни)\n"
    "• <code>1г-</code> — давнее года\n"
    "• <code>01.03.2025-01.09.2026</code> — между датами\n"
    "• <code>01.03.2025</code> — с этой даты по сегодня\n\n"
    "Единицы: д, н, м, г."
)
