"""Drives the real panel through aiogram's dispatcher with a fake Telegram.

Every button press and message below goes through the same routing, admin gate
and FSM that production uses; only the HTTP layer to Telegram is replaced, and
the userbot is a stub that records what it would have sent.

    DATABASE_URL=postgresql://... .venv/bin/python tests/test_wiring.py
"""
import asyncio
import itertools
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DSN = os.environ.get("DATABASE_URL", "").strip()
if not DSN:
    print("SKIP: DATABASE_URL not set")
    raise SystemExit(0)

ADMIN, STRANGER = 111, 999
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "x" * 32)
os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ["ADMIN_IDS"] = str(ADMIN)
os.environ["TG_ACCOUNT"] = "wiring"

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.methods import (  # noqa: E402
    AnswerCallbackQuery, EditMessageText, GetMe, SendMessage,
)
from aiogram.types import Chat, Message, Update, User  # noqa: E402

from tgsender import config as config_mod  # noqa: E402
from tgsender import settings as settings_mod  # noqa: E402
from tgsender.bot import AdminGate, Panel, router  # noqa: E402
from tgsender.db import DB, Recipient  # noqa: E402
from tgsender.filters import Filter  # noqa: E402
from tgsender.sender import SendWorker  # noqa: E402

ok = lambda m: print(f"  ok  {m}")
cfg = config_mod.load()
YEAR = 365.25 * 86400
ACCOUNT = f"wiring_{uuid.uuid4().hex[:8]}"
CHAT = Chat(id=ADMIN, type="private")
_ids = itertools.count(1)


class FakeTelegram(BaseSession):
    """Answers every Bot API call locally and remembers it."""

    def __init__(self):
        super().__init__()
        self.calls: list = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="Panel", username="panel_bot")
        if isinstance(method, SendMessage):
            # Real responses come back bound to the bot, so `.edit_text()` works.
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=CHAT, text=method.text).as_(bot)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    def screens(self) -> list[str]:
        """Texts the user actually saw, oldest first."""
        return [c.text for c in self.calls if isinstance(c, (SendMessage, EditMessageText))]

    def last_screen(self) -> str:
        return self.screens()[-1]

    def last_markup(self) -> list[str]:
        for c in reversed(self.calls):
            if isinstance(c, (SendMessage, EditMessageText)) and c.reply_markup:
                return [b.callback_data for row in c.reply_markup.inline_keyboard for b in row]
        return []

    def alerts(self) -> list[str]:
        return [c.text for c in self.calls
                if isinstance(c, AnswerCallbackQuery) and c.text]


class FakeUserbot:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, peer, text, **kw):
        self.sent.append((peer, text))


def update(bot, *, user=ADMIN, text=None, data=None):
    base_msg = {"message_id": next(_ids), "date": int(time.time()),
                "chat": {"id": user, "type": "private"}}
    sender = {"id": user, "is_bot": False, "first_name": "T"}
    if data is not None:
        payload = {"callback_query": {"id": str(next(_ids)), "from": sender,
                                      "chat_instance": "c", "data": data,
                                      "message": {**base_msg, "text": "…"}}}
    else:
        payload = {"message": {**base_msg, "from": sender, "text": text}}
    return Update.model_validate({"update_id": next(_ids), **payload}, context={"bot": bot})


async def main() -> None:
    db = await DB.connect(config_mod.normalize_dsn(DSN), ACCOUNT)
    tg = FakeTelegram()
    bot = Bot("123:ABC", session=tg)
    userbot = FakeUserbot()
    try:
        # A pace that finishes a tiny campaign quickly, in a zone where it is
        # midday right now, so quiet hours never interfere with the test.
        offset = 12 - datetime.now(timezone.utc).hour
        offset = (offset + 12) % 24 - 12
        tz = "UTC" if offset == 0 else f"Etc/GMT{'-' if offset > 0 else '+'}{abs(offset)}"
        await db.set_setting("timezone", tz)
        await db.set_setting("interval", "10")
        await db.set_setting("jitter", "0")

        now = time.time()
        await db.upsert_recipients([
            Recipient(1, 11, "anna", "Анна", None, now - 0.1 * YEAR),
            Recipient(2, 22, "nik", "Никита", "Сысоев", now - 0.5 * YEAR),
            Recipient(3, 33, "sasha", "Саша", None, now - 1.5 * YEAR),
            Recipient(4, 44, "masha", "Мария", None, now - 2.5 * YEAR),
            Recipient(5, 55, None, "Олег", None, None),
        ])

        worker = SendWorker(cfg, db, userbot)
        panel = Panel(cfg=cfg, db=db, client=userbot, worker=worker)
        gate = AdminGate(cfg.admin_ids, cfg.admin_usernames)
        router.message.filter(gate)
        router.callback_query.filter(gate)
        dp = Dispatcher()
        dp.include_router(router)

        async def press(data, user=ADMIN):
            await dp.feed_update(bot, update(bot, user=user, data=data), panel=panel)

        async def say(text, user=ADMIN):
            await dp.feed_update(bot, update(bot, user=user, text=text), panel=panel)

        # ---------------- access ----------------
        before = len(tg.calls)
        await say("/start", user=STRANGER)
        await press("count", user=STRANGER)
        assert len(tg.calls) == before, "a stranger gets no reply at all"
        await say("/start")
        assert "Панель рассылки" in tg.last_screen() and "<code>wiring</code>" in tg.last_screen(), tg.last_screen()
        assert "count" in tg.last_markup() and "set" in tg.last_markup()
        ok("strangers get silence; /start shows the panel with count and settings")

        # ---------------- 🔎 Посчитать ----------------
        await press("count")
        screen = tg.last_screen()
        assert "до 1 года" in screen and "Выбрано: 2" in screen, screen
        assert "♀ 1" in screen and "♂ 1" in screen
        ok("count opens the builder: default 'до 1 года', 2 people, split by gender")

        await press("f:p:3y")
        assert "Выбрано: 4" in tg.last_screen()
        await press("f:p:all")
        assert "Выбрано: 5" in tg.last_screen(), "'все' includes the undated dialogue"
        await press("f:g:m")
        await press("f:g:u")
        screen = tg.last_screen()
        assert "Выбрано: 2" in screen and "♀ женщины" in screen, screen
        await press("f:g:f")
        assert any("Хотя бы одна" in a for a in tg.alerts()), "cannot deselect every group"
        await press("f:g:m")
        await press("f:g:u")
        ok("presets and gender toggles change the count; the last group cannot be removed")

        await press("f:custom")
        assert "Напиши период" in tg.last_screen()
        await say("что-то непонятное")
        assert "⚠️" in tg.last_screen(), "bad period explains itself and waits"
        await say("1г-2г")
        screen = tg.last_screen()
        assert "от 1 г до 2 г" in screen and "Выбрано: 1" in screen, screen
        ok("typed period: bad input explained, '1г-2г' leaves only Саша")

        await press("f:who")
        screen = tg.last_screen()
        assert "Саша" in screen and "❔" in screen and "Анна" not in screen, screen
        await press("f:back")
        assert "Выбрано: 1" in tg.last_screen()
        ok("'кто попал' lists exactly the filtered people with their guessed gender")

        # ---------------- ⚙️ Настройки ----------------
        await press("set")
        assert "Настройки" in tg.last_screen() and "10 с" in tg.last_screen()
        await press("set:int")
        await press("set:int:120")
        assert (await db.get_settings())["interval"] == "120.0"
        assert "2 мин" in tg.last_screen()
        await press("set:jit")
        await press("set:jit:custom")
        await say("95")
        assert "⚠️" in tg.last_screen()
        await say("20")
        assert (await db.get_settings())["jitter"] == "0.2" and "±20%" in tg.last_screen()
        s = await settings_mod.load(db, cfg)
        future = s.now() + timedelta(days=2)
        await press("set:dl")
        await say(future.strftime("%d.%m.%Y 21:00"))
        s = await settings_mod.load(db, cfg)
        assert s.deadline.date() == future.date() and s.deadline.hour == 21
        await press("set:dl")
        await say("01.01.2020 10:00")
        assert "прошёл" in tg.last_screen(), "a past deadline is refused"
        ok("settings: preset interval, custom jitter with validation, deadline typed")

        await press("set:int:10")
        await press("set:jit:0")

        # ---------------- count → campaign → launch ----------------
        await press("count")
        await press("f:p:all")
        await press("f:use")
        assert "Пришли следующим сообщением текст" in tg.last_screen()
        await say("Приглашение на вечеринку 🎉")
        screen = tg.last_screen()
        assert "Проверь перед запуском" in screen and "Получателей: <b>5</b>" in screen, screen
        assert "Успеваем" in screen and "go" in tg.last_markup()
        ok("'разослать этим людям' → text → launch screen with pace and deadline check")

        await press("go")
        assert "запущена" in tg.last_screen() and worker.running
        # Five people 10 s apart is ~40 s; stop once the first one is out.
        for _ in range(100):
            if userbot.sent or not worker.running:
                break
            await asyncio.sleep(0.1)
        if worker.running:
            worker.request_stop()
            await worker.task
        assert userbot.sent, "the userbot actually sent"
        assert userbot.sent[0][1] == "Приглашение на вечеринку 🎉"
        progress = await db.progress(worker.campaign_id)
        assert progress.get("sent", 0) == len(userbot.sent)
        ok(f"launch sends through the userbot ({len(userbot.sent)} before stop), "
           f"progress matches")

        await press("status")
        assert f"#{worker.campaign_id}" in tg.last_screen()
        assert "все диалоги" in tg.last_screen(), "status shows the campaign's filter"
        await say("/start")
        assert "resume" in tg.last_markup(), "a stopped campaign offers Продолжить"
        ok("status shows the filter; stopped campaign offers resume")

        # ---------------- stale buttons and stray text ----------------
        await press("go")
        assert any("устарела" in a for a in tg.alerts())
        await press("f:p:1y")
        await say("привет")
        assert "Выбери действие" in tg.last_screen()
        ok("stale buttons and unexpected text get an answer, not silence")

        # ---------------- deadline stops sending ----------------
        await db.set_setting("deadline", (s.now() - timedelta(minutes=1))
                             .replace(tzinfo=None).isoformat(timespec="minutes"))
        await press("resume")
        assert any("Дедлайн уже прошёл" in a for a in tg.alerts()), tg.alerts()
        await db.set_setting("deadline", (s.now() + timedelta(days=1))
                             .replace(tzinfo=None).isoformat(timespec="minutes"))
        ok("resume refuses when the deadline has passed")

        stored = Filter.from_json((await db.get_campaign(worker.campaign_id))["filter_json"])
        assert stored.label == "все диалоги"
        ok("the campaign row keeps the filter it was launched with")

        # ---------------- 📝 Новая рассылка: text → builder → next ----------------
        await press("new")
        assert "Пришли следующим сообщением" in tg.last_screen()
        await say("Второе приглашение")
        assert "Кому отправляем" in tg.last_screen() and "f:next" in tg.last_markup()
        await press("f:g:m")
        await press("f:g:u")
        await press("f:next")
        screen = tg.last_screen()
        assert "Проверь перед запуском" in screen and "♀ женщины" in screen, screen
        await press("f:back")
        assert "Кому отправляем" in tg.last_screen(), "back from launch returns to builder"
        await press("cancel")
        ok("new campaign: text first, then the builder, then the launch screen")

        # ---------------- the sender stops at the deadline ----------------
        await db.set_setting("deadline", (s.now() - timedelta(minutes=1))
                             .replace(tzinfo=None).isoformat(timespec="minutes"))
        before = len(userbot.sent)
        cid = await db.create_campaign("x", None, Filter(), ADMIN, 10.0,
                                       exclude_sent=False)
        worker.start(cid)
        await worker.task
        row = await db.get_campaign(cid)
        assert row["status"] == "stopped" and "Дедлайн" in row["stop_reason"], dict(row)
        assert len(userbot.sent) == before, "nothing is sent after the deadline"
        assert (await db.progress(cid)).get("pending") == 5, "nobody lost, all resumable"
        ok("past the deadline the sender stops before sending, keeping everyone pending")

    finally:
        if worker.running:
            worker.request_stop()
            await worker.task
        async with db.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM deliveries WHERE campaign_id IN "
                "(SELECT id FROM campaigns WHERE account = $1)", ACCOUNT)
            for table in ("campaigns", "recipients", "settings"):
                await conn.execute(f"DELETE FROM {table} WHERE account = $1", ACCOUNT)
        await db.close()


asyncio.run(main())
print("\nWIRING OK")
