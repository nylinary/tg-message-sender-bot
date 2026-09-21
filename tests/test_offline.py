"""Pure-logic tests. No database, no network:

    .venv/bin/python tests/test_offline.py
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("ADMIN_IDS", "111, 222")
os.environ["TG_ACCOUNT"] = "selftest"

from tgsender import bot as bot_mod  # noqa: E402
from tgsender import config as config_mod  # noqa: E402
from tgsender import scheduling as sch  # noqa: E402
from tgsender import settings as st  # noqa: E402
from tgsender.filters import (  # noqa: E402
    ALL_GENDERS, DAY, Filter, PeriodError, parse_period, preset,
)
from tgsender.gender import guess  # noqa: E402

ok = lambda m: print(f"  ok  {m}")
cfg = config_mod.load()
MSK = ZoneInfo("Europe/Moscow")


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        return True
    raise AssertionError(f"{fn.__name__}{a} should have raised {exc.__name__}")


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

assert cfg.admin_ids == frozenset({111, 222}) and cfg.admin_usernames == frozenset()
assert cfg.pacing.interval == 180 and cfg.timezone == "Europe/Moscow"
assert cfg.deadline.tzinfo is None, "config deadline is wall-clock, zone applied later"
assert config_mod.normalize_dsn("postgres://a:b@h/d") == "postgresql://a:b@h/d"
assert config_mod.normalize_dsn("postgresql+asyncpg://a@h/d") == "postgresql://a@h/d"
ok("config: admins, 180 s default interval, Moscow timezone, DSN normalisation")

blank = config_mod.Config(**{**cfg.__dict__, "database_url": ""})
raises(SystemExit, config_mod.require_database_url, blank)
ok("a missing DATABASE_URL is rejected")

parse = config_mod.parse_admins
assert parse(" 111 , @Nylinary , some_friend ") == (
    frozenset({111}), frozenset({"nylinary", "some_friend"})
)
for bad in ("@abc", "1nvalid", "has-a-dash", "@@x", "", " , "):
    raises(SystemExit, parse, bad)
ok("ADMIN_IDS: ids and usernames mixed; malformed or empty rejected")


class _U:
    def __init__(self, uid, username=None):
        self.id, self.username = uid, username


class _Ev:
    def __init__(self, user):
        self.from_user = user


async def _gate():
    g = bot_mod.AdminGate({111}, {"nylinary"})
    assert await g(_Ev(_U(111))) and not await g(_Ev(_U(9, "x"))) and not await g(_Ev(None))
    assert await g(_Ev(_U(777, "NyLinary"))) and 777 in g.ids and not g.unresolved
    assert await g(_Ev(_U(777)))

asyncio.run(_gate())
ok("AdminGate: ids pass, usernames pin their id, strangers refused")

# --------------------------------------------------------------------------- #
# gender
# --------------------------------------------------------------------------- #

cases = [
    ("Анна", None, "f"), ("Никита", "Сысоев", "m"), ("Саша", "Петрова", "f"),
    ("Саша", "Петров", "m"), ("Саша", None, "u"), ("Женя", None, "u"),
    ("Мария Иванова", None, "f"), ("Иванова", "Мария", "f"), ("🔥Макс🔥", None, "m"),
    ("Kostya", None, "m"), ("Anastasia", "K.", "f"), ("Айгерим", None, "f"),
    ("Ерлан", "Нурланұлы", "m"), ("Лёша", None, "m"), ("Илья", None, "m"),
    ("Кузнецова", None, "f"), ("Tom", "Smith", "m"), ("Гоша", None, "m"),
    ("Милана", None, "f"), ("Dmitrii", "Ivanov", "m"), ("123", None, "u"),
    (None, None, "u"), ("", "", "u"),
]
wrong = [(f, l, guess(f, l), e) for f, l, e in cases if guess(f, l) != e]
assert not wrong, wrong
ok(f"gender guess right on {len(cases)} tricky names, incl. Саша/Женя → unknown")

# --------------------------------------------------------------------------- #
# periods
# --------------------------------------------------------------------------- #

now = datetime(2026, 9, 21, 15, 0, tzinfo=MSK)
ts = now.timestamp()

since, until, label = parse_period("2г", now)
assert until is None and abs((ts - since) / DAY - 730.5) < 0.01 and label == "до 2 г"
since, until, label = parse_period("30д-1г", now)
assert abs((ts - until) / DAY - 30) < 1e-6 and abs((ts - since) / DAY - 365.25) < 1e-6
assert label == "от 30 дн до 1 г"
since, until, _ = parse_period("1г-30д", now)
assert abs((ts - until) / DAY - 30) < 1e-6, "reversed range is swapped, not rejected"
since, until, _ = parse_period("7-90", now)
assert abs((ts - until) / DAY - 7) < 1e-6 and abs((ts - since) / DAY - 90) < 1e-6
since, until, label = parse_period("от 30 дней до 2 лет", now)
assert abs((ts - until) / DAY - 30) < 1e-6 and label.startswith("от 30")
since, until, label = parse_period("1г-", now)
assert since is None and abs((ts - until) / DAY - 365.25) < 1e-6
ok("relative periods: single value, ranges, reversed, words, open-ended")

since, until, label = parse_period("01.03.2025-01.09.2026", now)
assert datetime.fromtimestamp(since, MSK) == datetime(2025, 3, 1, tzinfo=MSK)
assert datetime.fromtimestamp(until, MSK) == datetime(2026, 9, 1, 23, 59, 59, tzinfo=MSK)
since, until, label = parse_period("01.03.2025", now)
assert until is None and label == "с 01.03.2025"
since, until, _ = parse_period("01.09 - 01.03", now)
assert since < until, "reversed dates are swapped"
ok("absolute periods: between dates (inclusive), since a date, reversed")

for bad in ("", "abc", "5x", "1-2-3", "31.02.2025", "10лет-5кг"):
    raises(PeriodError, parse_period, bad, now)
ok("nonsense periods rejected with a readable message")

since, until, label = preset("1y", now)
assert until is None and abs((ts - since) / DAY - 365) < 1e-6 and label == "до 1 года"
assert preset("all", now) == (None, None, "все диалоги")
ok("period presets")

f = Filter().with_period(1.0, 2.0, "x")
assert f.dated and f.all_genders
f2 = f.toggle("u")
assert f2.genders == frozenset({"f", "m"}) and f2.genders_text() == "♀ женщины + ♂ мужчины"
lone = Filter(genders=frozenset({"f"}))
assert lone.toggle("f").genders == frozenset({"f"}), "cannot empty the selection"
assert Filter.from_json(f2.to_json()) == f2 and Filter.from_json(None) == Filter()
assert not Filter().dated and Filter().genders == ALL_GENDERS
ok("Filter: toggles, never empty, JSON round-trip")

# --------------------------------------------------------------------------- #
# settings parsing
# --------------------------------------------------------------------------- #

assert st.parse_deadline("25.09.2026 21:00", now) == datetime(2026, 9, 25, 21, 0)
assert st.parse_deadline("25.09 21:00", now) == datetime(2026, 9, 25, 21, 0)
assert st.parse_deadline("25.09", now) == datetime(2026, 9, 25, 23, 59)
assert st.parse_deadline("01.01 12:00", now) == datetime(2027, 1, 1, 12, 0), "rolls to next year"
for bad in ("20.09.2026 10:00", "32.09", "tomorrow", "25.09.2030"):
    raises(st.SettingError, st.parse_deadline, bad, now)
ok("deadline: with/without year and time, past and far-future rejected")

assert st.parse_interval("180", 10) == 180 and st.parse_interval("3м", 10) == 180
assert st.parse_interval("2м30с", 10) == 150 and st.parse_interval("1.5 мин", 10) == 90
for bad in ("5", "abc", "3кг", "", "10ч"):
    raises(st.SettingError, st.parse_interval, bad, 10)
ok("interval: seconds, minutes, mixed; below floor and nonsense rejected")

assert st.parse_jitter("35") == 0.35 and st.parse_jitter("35%") == 0.35
assert st.parse_jitter("0.35") == 0.35 and st.parse_jitter("0") == 0
for bad in ("95", "-1", "x"):
    raises(st.SettingError, st.parse_jitter, bad)
ok("jitter: percent, fraction, bounds")

assert st.parse_timezone("Europe/Moscow") == "Europe/Moscow"
assert st.parse_timezone("UTC+3") == "Etc/GMT-3", "Etc/GMT signs are inverted"
assert st.parse_timezone("+5") == "Etc/GMT-5" and st.parse_timezone("utc") == "UTC"
for bad in ("Mars/Olympus", "UTC+20"):
    raises(st.SettingError, st.parse_timezone, bad)
for _, zone in st.TIMEZONE_PRESETS:
    ZoneInfo(zone)
ok("timezone: IANA names, UTC offsets, every preset exists")

s = st.resolve({}, cfg)
assert s.interval == 180 and s.jitter == 0.35 and s.timezone == "Europe/Moscow"
assert s.deadline == datetime(2026, 9, 25, 21, 0, tzinfo=MSK)
s = st.resolve({"interval": "60", "jitter": "0.1", "timezone": "Asia/Almaty",
                "deadline": "2026-09-24T20:00"}, cfg)
assert s.interval == 60 and s.jitter == 0.1
assert s.deadline == datetime(2026, 9, 24, 20, 0, tzinfo=ZoneInfo("Asia/Almaty")), (
    "stored wall time is read in the stored timezone"
)
s = st.resolve({"interval": "1", "jitter": "5", "timezone": "Nope/Nope", "deadline": "?"}, cfg)
assert s.interval == cfg.pacing.min_interval and s.jitter == st.MAX_JITTER
assert s.timezone == "Europe/Moscow" and s.deadline.hour == 21
assert st.resolve({}, cfg).night_mode == "silent" and not st.resolve({}, cfg).pause_at_night
assert st.resolve({"night_mode": "pause"}, cfg).pause_at_night
assert st.resolve({"night_mode": "loud"}, cfg).night_mode == "silent", "unknown mode → default"
ok("settings: defaults, stored overrides, corrupt values fall back safely")

# --------------------------------------------------------------------------- #
# quiet hours and pace estimate
# --------------------------------------------------------------------------- #

assert sch.is_quiet(2, 23, 10) and sch.is_quiet(23, 23, 10) and not sch.is_quiet(15, 23, 10)
assert not sch.is_quiet(5, 0, 0) and sch.quiet_hours_per_day(23, 10) == 11
base = datetime(2026, 9, 21, 12, 0, tzinfo=MSK)
assert sch.active_seconds(base, base + timedelta(days=1), 23, 10) == 13 * 3600
assert sch.active_seconds(base, base - timedelta(hours=1), 23, 10) == 0
assert sch.advance(base, 3600, 23, 10) == base + timedelta(hours=1)
assert sch.advance(base.replace(hour=22, minute=30), 3600, 23, 10) == base.replace(
    day=22, hour=10, minute=30
), "advance skips the night"
ok("quiet hours: wrap past midnight, active time, advance over the night")

deadline = base + timedelta(days=4)
p, r = cfg.pacing, cfg.risk
e = sch.estimate(100, 180, 0.35, deadline, p, r, base)
assert e.fits and e.capacity == 100 and e.risk == sch.GREEN
assert abs(e.per_hour - 20) < 1e-9 and e.finishes_at < deadline
ok(f"100 people at 180 s: fits, ~{e.per_day:.0f}/day, done {e.finishes_at:%d.%m %H:%M}")

e = sch.estimate(3000, 180, 0.35, deadline, p, r, base)
assert not e.fits and 0 < e.capacity < 3000 and e.missed == 3000 - e.capacity
text = sch.describe(e, p, r, "Europe/Moscow")
assert "Не успеваем" in text and "нужен интервал" in text
ok(f"3000 people at 180 s: reaches ~{e.capacity}, says how to fix it")

assert sch.estimate(100, 20, 0, deadline, p, r, base).risk == sch.RED
assert sch.estimate(100, 100, 0, deadline, p, r, base).risk == sch.YELLOW
past = sch.estimate(50, 180, 0, base - timedelta(hours=1), p, r, base)
assert past.capacity == 0 and "уже прошёл" in sch.describe(past, p, r, "UTC")
assert sch.estimate(0, 180, 0, deadline, p, r, base).capacity == 0
huge = sch.estimate(10**6, 10, 0, deadline, p, r, base)
assert "Даже на минимальном" in sch.describe(huge, p, r, "UTC")
for n in (0, 1, 2, 100, 5000):
    sch.describe(sch.estimate(n, 180, 0.35, deadline, p, r, base), p, r, "UTC")
ok("estimate: risk bands, past deadline, zero, impossible volume")

paused = sch.estimate(10**6, 180, 0.35, deadline, p, r, base, pause_at_night=True)
nightly = sch.estimate(10**6, 180, 0.35, deadline, p, r, base, pause_at_night=False)
assert nightly.capacity > paused.capacity * 1.7, (paused.capacity, nightly.capacity)
assert abs(nightly.per_day / paused.per_day - 24 / 13) < 1e-6
assert "шлём без звука" in sch.describe(nightly, p, r, "UTC", pause_at_night=False)
assert "не шлём" in sch.describe(paused, p, r, "UTC", pause_at_night=True)
late = base.replace(hour=22, minute=30)
assert sch.estimate(3, 1800, 0, deadline, p, r, late, pause_at_night=False).finishes_at == \
    late + timedelta(hours=1), "sending through the night does not skip it"
ok(f"night silent: 24 h of sending, reach {paused.capacity} → {nightly.capacity} by deadline")

# --------------------------------------------------------------------------- #
# panel helpers and error classes
# --------------------------------------------------------------------------- #

assert bot_mod.resolve_parse_mode("hi", "hi") == ("hi", None)
assert bot_mod.resolve_parse_mode("<b>hi</b>", "hi") == ("<b>hi</b>", "html")
assert len(bot_mod.bar(3, 7)) == 16 and len(bot_mod.bar(0, 0)) == 16
ok("formatting kept only when Telethon can parse it; progress bar fixed width")

from telethon import errors  # noqa: E402

from tgsender import sender as sender_mod  # noqa: E402

assert errors.PeerFloodError in sender_mod.ACCOUNT_ERRORS
assert errors.UserPrivacyRestrictedError in sender_mod.PERMANENT_ERRORS
assert errors.FloodWaitError not in sender_mod.PERMANENT_ERRORS
assert not set(sender_mod.ACCOUNT_ERRORS) & set(sender_mod.PERMANENT_ERRORS)
ok("error classes are disjoint and correctly assigned")

# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #

from telethon.tl.types import auth as tl_auth  # noqa: E402

from tgsender import login  # noqa: E402

n = login.normalize_phone
assert n("89921873379") == "+79921873379", "domestic 8… becomes +7…"
assert n("8 (992) 187-33-79") == "+79921873379"
assert n("79921873379") == n("+7 992 187 33 79") == n("9921873379") == "+79921873379"
assert n("+77011234567") == "+77011234567" and n("+380501234567") == "+380501234567"
for bad in ("123", "8646649663:AAFA-viykbPjBM", "abc"):
    raises(login.LoginError, n, bad)
ok("phone: 8…, spaces, brackets, bare mobile → +7…; bot tokens and junk refused")


class FakeLoginClient:
    """Scripted Telegram: each sign_in pops the next outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.hashes_sent, self.sign_ins = [], []

    async def connect(self):
        pass

    async def send_code_request(self, phone):
        h = f"hash{len(self.hashes_sent)}"
        self.hashes_sent.append(h)
        return tl_auth.SentCode(type=tl_auth.SentCodeTypeApp(length=5), phone_code_hash=h)

    async def sign_in(self, phone=None, code=None, *, password=None, phone_code_hash=None):
        self.sign_ins.append((phone, code, password, phone_code_hash))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome(request=None)
        return outcome


def scripted(answers):
    it = iter(answers)
    return lambda prompt="": next(it)


async def _login_checks():
    # The user's exact failure: 8… number, one bad code, then the right one.
    c = FakeLoginClient([errors.PhoneCodeInvalidError, "ME"])
    me = await login.interactive_login(
        c, ask=scripted(["89921873379", "41653", "4 1 6 5 4"]), ask_secret=scripted([])
    )
    assert me == "ME"
    assert all(p == "+79921873379" for p, *_ in c.sign_ins), c.sign_ins
    assert all(h == "hash0" for *_, h in c.sign_ins), "the hash is passed on every try"
    assert c.sign_ins[-1][1] == "41654", "spaces in the code are ignored"

    # Expired code → a new one is requested and its hash is used.
    c = FakeLoginClient([errors.PhoneCodeExpiredError, "ME"])
    await login.interactive_login(c, ask=scripted(["+79921873379", "11111", "22222"]),
                                  ask_secret=scripted([]))
    assert c.hashes_sent == ["hash0", "hash1"] and c.sign_ins[-1][3] == "hash1"

    # Two-step verification, one wrong password first.
    c = FakeLoginClient([errors.SessionPasswordNeededError,
                         errors.PasswordHashInvalidError, "ME"])
    me = await login.interactive_login(c, ask=scripted(["+79921873379", "12345"]),
                                       ask_secret=scripted(["wrong", "right"]))
    assert me == "ME" and c.sign_ins[-1][2] == "right"

    # A pasted bot token is refused at the phone prompt, then a number works.
    c = FakeLoginClient(["ME"])
    await login.interactive_login(
        c, ask=scripted(["8646649663:AAFA-viykbPjBM", "89921873379", "12345"]),
        ask_secret=scripted([]),
    )
    assert c.sign_ins[0][0] == "+79921873379"

    # Three wrong codes end cleanly with advice, not a traceback.
    c = FakeLoginClient([errors.PhoneCodeInvalidError] * 3)
    try:
        await login.interactive_login(c, ask=scripted(["+79921873379", "1", "2", "3"]),
                                      ask_secret=scripted([]))
    except login.LoginError as exc:
        assert "заново" in str(exc)
    else:
        raise AssertionError("three bad codes must stop")

asyncio.run(_login_checks())
ok("login: bad code retried with the same hash, expiry re-sends, 2FA, token refused")

# --------------------------------------------------------------------------- #
# @SpamBot
# --------------------------------------------------------------------------- #

from tgsender import spamcheck as sc  # noqa: E402

replies = {
    "Good news, no limits are currently applied to your account. You’re free as a bird!":
        ("ok", None),
    "Ваш аккаунт свободен от каких-либо ограничений.": ("ok", None),
    ("I’m afraid some Telegram users found your messages annoying and forwarded them "
     "to our team of moderators for inspection. The moderators have confirmed the report "
     "and your account is now limited until 28 Sep 2026, 14:02 UTC.\n\nWhile the account "
     "is limited, you will not be able to send messages to people who do not have your "
     "number in their phone contacts."): ("limited", "28 Sep 2026, 14:02 UTC"),
    ("Unfortunately, some actions can trigger a harsh response from our anti-spam systems. "
     "If you think your account was limited by mistake, you can submit a complaint."):
        ("limited", None),
    ("К сожалению, пользователи пожаловались на ваши сообщения. Ваш аккаунт ограничен "
     "до 28 сент. 2026, 14:02 UTC."): ("limited", "28 сент. 2026, 14:02 UTC"),
    "Hello! Choose an option below.": ("unknown", None),
    # The reply that went out in production, verbatim.
    ("Здравствуйте, Александр. К сожалению, кто-то из пользователей Телеграма посчитал Ваши "
     "сообщения нежелательными и переслал их на проверку команде модераторов. Модераторы "
     "подтвердили, что жалоба была обоснованной.\n\nВаш аккаунт временно ограничен: Вы не "
     "можете писать тем, кто не сохранил Ваш номер в список контактов, а также приглашать таких "
     "пользователей в группы или каналы. Если незнакомый пользователь напишет Вам первым, Вы "
     "сможете ему ответить.\n\nОграничения будут автоматически сняты 21 Sep 2026, 20:36 UTC "
     "(по московскому времени — на три часа позже). Обратите внимание, что если пользователи "
     "будут жаловаться на новые нежелательные сообщения от Вас, в следующий раз аккаунт будет "
     "ограничен на больший срок."): ("limited", "21 Sep 2026, 20:36 UTC"),
}
for text, expected in replies.items():
    assert sc.classify(text) == expected, (text[:40], sc.classify(text), expected)
ok("SpamBot replies: ok / limited with date / limited indefinitely / unknown, EN + RU")

ts = sc.until_timestamp("21 Sep 2026, 20:36 UTC")
assert datetime.fromtimestamp(ts, MSK) == datetime(2026, 9, 21, 23, 36, tzinfo=MSK), "UTC → MSK"
assert sc.until_timestamp("28 сент. 2026, 14:02 UTC") is not None
assert sc.until_timestamp(None) is None and sc.until_timestamp("soon") is None
st_ = sc.SpamStatus("limited", "21 Sep 2026, 20:36 UTC", "x", 1.0, ts)
assert sc.SpamStatus.from_json(st_.to_json()) == st_ and sc.SpamStatus.from_json("{") is None
assert st_.headline(MSK) == "аккаунт ОГРАНИЧЕН до 21.09 23:36", st_.headline(MSK)
old = sc.SpamStatus.from_json('{"state":"limited","until":null,"text":"x","checked_at":1}')
assert old is not None and "срок не указан" in old.headline(), "rows saved before this change load"
ok("the limit's end date is read from 'будут сняты', shown in local time")
assert st.resolve({}, cfg).spam_every_hours == 6 and st.resolve({}, cfg).spam_notify == "always"
assert st.resolve({"spam_every_hours": "0", "spam_notify": "problems"}, cfg).spam_every_hours == 0
assert st.resolve({"spam_every_hours": "x", "spam_notify": "??"}, cfg).spam_notify == "always"
ok("SpamStatus round-trips; schedule settings default to every 6 h, notify always")


class _Msg:
    def __init__(self, id, out, message):
        self.id, self.out, self.message = id, out, message


class FakeSpamClient:
    def __init__(self, reply_parts):
        self.reply_parts = reply_parts
        self.sent_to = []

    async def send_message(self, peer, text):
        self.sent_to.append((peer, text))
        return _Msg(100, True, text)

    async def get_messages(self, peer, limit=5):
        old = _Msg(99, False, "an older answer")
        mine = _Msg(100, True, "/start")
        return [_Msg(101 + i, False, t) for i, t in enumerate(self.reply_parts)] + [mine, old]


async def _ask_checks():
    c = FakeSpamClient(["part one", "part two"])
    text = await sc.ask_spambot(c, timeout=1, poll=0.01)
    assert c.sent_to == [("SpamBot", "/start")]
    assert text == "part one\n\npart two", "only replies newer than our /start, in order"
    try:
        await sc.ask_spambot(FakeSpamClient([]), timeout=0.05, poll=0.01)
    except sc.SpamCheckError:
        pass
    else:
        raise AssertionError("no answer must time out")

asyncio.run(_ask_checks())
ok("ask_spambot: sends /start, ignores older and own messages, times out cleanly")

print("\nALL OFFLINE TESTS PASSED")
