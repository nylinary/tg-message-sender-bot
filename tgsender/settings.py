"""Runtime settings the panel can change: deadline, pace, jitter, timezone.

Defaults come from config.toml; anything changed in ⚙️ Настройки is stored in
Postgres per account and wins. The sender re-reads these before every message,
so a change applies to a campaign that is already running.

The deadline is stored as local wall-clock time without an offset and combined
with the timezone setting when read, so "21:00" keeps meaning 21:00 where you
are even if you change the timezone afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Config

MAX_INTERVAL = 6 * 3600
MAX_JITTER = 0.9

NIGHT_SILENT, NIGHT_PAUSE = "silent", "pause"
NIGHT_MODES = {
    NIGHT_SILENT: "ночью отправлять без звука",
    NIGHT_PAUSE: "ночью не отправлять",
}


class SettingError(ValueError):
    """Raised with a message that can be shown to the user as-is."""


@dataclass(frozen=True)
class Settings:
    deadline: datetime   # timezone-aware
    interval: float      # seconds between messages
    jitter: float        # fraction, 0.35 = ±35%
    timezone: str
    night_mode: str = NIGHT_SILENT

    @property
    def pause_at_night(self) -> bool:
        return self.night_mode == NIGHT_PAUSE

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    @property
    def gap_range(self) -> tuple[float, float]:
        return self.interval * (1 - self.jitter), self.interval * (1 + self.jitter)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SettingError(f"Неизвестный часовой пояс «{name}».") from exc


def resolve(raw: dict[str, str], cfg: Config) -> Settings:
    """Merge stored values over config defaults, ignoring anything corrupt."""
    timezone = raw.get("timezone") or cfg.timezone
    try:
        tz = _zone(timezone)
    except SettingError:
        timezone, tz = cfg.timezone, _zone(cfg.timezone)

    try:
        wall = datetime.fromisoformat(raw["deadline"]) if raw.get("deadline") else None
    except ValueError:
        wall = None
    wall = (wall or cfg.deadline).replace(tzinfo=None)

    def number(key: str, default: float) -> float:
        try:
            return float(raw[key]) if key in raw else default
        except ValueError:
            return default

    interval = min(MAX_INTERVAL, max(cfg.pacing.min_interval,
                                     number("interval", cfg.pacing.interval)))
    jitter = min(MAX_JITTER, max(0.0, number("jitter", cfg.pacing.jitter)))
    default_night = cfg.pacing.night_mode if cfg.pacing.night_mode in NIGHT_MODES else NIGHT_SILENT
    night = raw.get("night_mode") if raw.get("night_mode") in NIGHT_MODES else default_night
    return Settings(wall.replace(tzinfo=tz), interval, jitter, timezone, night)


async def load(db, cfg: Config) -> Settings:
    return resolve(await db.get_settings(), cfg)


# --------------------------------------------------------------------------- #
# parsing what people type into the panel
# --------------------------------------------------------------------------- #

_DT = re.compile(
    r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?:[ ,]+(\d{1,2})[:.](\d{2}))?$"
)


def parse_deadline(text: str, now: datetime) -> datetime:
    """'25.09.2026 21:00', '25.09 21:00', '25.09' -> naive local wall time."""
    m = _DT.match(text.strip())
    if not m:
        raise SettingError("Формат: 25.09.2026 21:00 (время можно не писать).")
    day, month = int(m.group(1)), int(m.group(2))
    year = int(m.group(3)) if m.group(3) else now.year
    if year < 100:
        year += 2000
    hour = int(m.group(4)) if m.group(4) else 23
    minute = int(m.group(5)) if m.group(5) else (0 if m.group(4) else 59)
    try:
        moment = datetime(year, month, day, hour, minute)
    except ValueError as exc:
        raise SettingError("Такой даты или времени нет.") from exc
    naive_now = now.replace(tzinfo=None)
    if not m.group(3) and moment <= naive_now:
        moment = moment.replace(year=moment.year + 1)
    if moment <= naive_now:
        raise SettingError("Дедлайн уже прошёл — нужна дата в будущем.")
    if moment - naive_now > timedelta(days=366):
        raise SettingError("Дедлайн дальше года — проверь дату.")
    return moment


def parse_interval(text: str, floor: float) -> float:
    """'180', '180с', '3м', '3 мин', '2м30с' -> seconds."""
    raw = text.strip().lower().replace(" ", "").replace(",", ".")
    total, matched = 0.0, False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)([a-zа-я]*)", raw):
        matched = True
        v = float(value)
        if unit in ("", "с", "сек", "s", "sec"):
            total += v
        elif unit in ("м", "мин", "m", "min"):
            total += v * 60
        elif unit in ("ч", "h"):
            total += v * 3600
        else:
            raise SettingError(f"Неизвестная единица «{unit}». Используй с, м или ч.")
    if not matched or re.sub(r"\d+(?:\.\d+)?[a-zа-я]*", "", raw):
        raise SettingError("Напиши число секунд, например 180, или 3м.")
    if total < floor:
        raise SettingError(f"Меньше {floor:g} с нельзя — Telegram ответит FLOOD_WAIT.")
    if total > MAX_INTERVAL:
        raise SettingError("Больше 6 часов между сообщениями — это вряд ли то, что нужно.")
    return total


def parse_jitter(text: str) -> float:
    """'35', '35%', '0.35' -> 0.35."""
    raw = text.strip().replace("%", "").replace(",", ".")
    try:
        value = float(raw)
    except ValueError as exc:
        raise SettingError("Напиши процент, например 35.") from exc
    if "." in raw and 0 < value < 1:
        value *= 100
    if not 0 <= value <= MAX_JITTER * 100:
        raise SettingError(f"Джиттер от 0 до {MAX_JITTER * 100:g}%.")
    return value / 100


_OFFSET = re.compile(r"^(?:utc|gmt)?\s*([+-])\s*(\d{1,2})$")


def parse_timezone(text: str) -> str:
    """'Europe/Moscow', 'UTC+3', '+5' -> an IANA zone name."""
    raw = text.strip()
    m = _OFFSET.match(raw.lower())
    if m:
        hours = int(m.group(2))
        if hours > 14:
            raise SettingError("Смещение больше 14 часов не бывает.")
        # Etc/GMT names have the sign inverted: UTC+3 is Etc/GMT-3.
        name = "UTC" if hours == 0 else f"Etc/GMT{'-' if m.group(1) == '+' else '+'}{hours}"
    elif raw.lower() in ("utc", "gmt"):
        name = "UTC"
    else:
        name = raw
    _zone(name)
    return name


TIMEZONE_PRESETS: list[tuple[str, str]] = [
    ("Калининград", "Europe/Kaliningrad"),
    ("Москва", "Europe/Moscow"),
    ("Самара", "Europe/Samara"),
    ("Екатеринбург", "Asia/Yekaterinburg"),
    ("Новосибирск", "Asia/Novosibirsk"),
    ("Алматы", "Asia/Almaty"),
    ("Ташкент", "Asia/Tashkent"),
    ("Минск", "Europe/Minsk"),
    ("Киев", "Europe/Kyiv"),
    ("Тбилиси", "Asia/Tbilisi"),
    ("Дубай", "Asia/Dubai"),
    ("UTC", "UTC"),
]
INTERVAL_PRESETS = [60, 120, 180, 300, 600]
JITTER_PRESETS = [0, 15, 25, 35, 50]
