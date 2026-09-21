# tgsender

A private Telegram bot that mails one invite, unchanged, to people you already
have a dialogue with — sent from your own account, filtered by how recently you
spoke and by (guessed) gender, paced at an interval you choose, and stopped at
a deadline.

Two Telegram identities, two different jobs:

- **The control bot** (`BOT_TOKEN`, from @BotFather) is the panel you and your
  friends press buttons in. It is whitelisted and never messages a guest.
- **The userbot** (`TG_API_ID` / `TG_API_HASH` / `TG_SESSION`, your personal
  account over MTProto) does the sending. Bots cannot start conversations,
  which is why the sending has to come from your account.

## The panel

```
/start
 ├─ 📝 Новая рассылка → текст → фильтр → ➡️ Далее → проверка → 🚀 Запустить
 ├─ 🔎 Посчитать получателей → тот же фильтр, без запуска
 │                              └─ 📝 Разослать этим людям → текст → проверка
 ├─ ▶️ Продолжить #N          (если рассылка была остановлена)
 ├─ 📊 Статус  ·  ⚙️ Настройки
 └─ 🔄 Пересканировать диалоги
```

### The filter

One builder serves both 🔎 Посчитать and 📝 Новая рассылка, so the number you
count is exactly the list a campaign freezes.

- **Период последнего диалога** — buttons for 3/6 months and 1/2/3 years, «Все»,
  or ✏️ type your own:

  | You type | Means |
  | --- | --- |
  | `2г` | last 2 years |
  | `30д-1г` | between 30 days and 1 year ago |
  | `7-90` | between 7 and 90 days ago (no unit = days) |
  | `1г-` | longer ago than a year |
  | `01.03.2025-01.09.2026` | between two dates, inclusive |
  | `01.03.2025` | since that date |

  Units: `д`, `н`, `м`, `г`. Dialogues Telegram gave no date for appear only
  under «Все».

- **Пол** — three toggles, ♀ / ♂ / ❔, any combination except none. The screen
  always shows all three counts for the chosen period, so you can see what each
  toggle adds.

- **👀 Кто попал** — 25 random people from the current selection with their
  guessed gender and last-dialogue date. Use it to judge how wrong the gender
  guess is on *your* contacts before trusting it.

People who already received an earlier campaign are excluded automatically,
and the screen says how many.

### Gender is a guess

Telegram has no gender field. `tgsender/gender.py` guesses from the display
name, strongest evidence first:

1. a known first name — Russian, Kazakh and common Latin spellings, with
   diminutives (Анна, Маша, Айгерим, Kostya…)
2. a gendered surname or patronymic — `-ова/-ов`, `-ская/-ский`, `-овна/-ович`,
   `-қызы/-ұлы`
3. the shape of the first word — `-а/-я` → ♀, consonant → ♂, skipping male
   names like Никита and Илья

Genuinely ambiguous names (Саша, Женя, Валя, Слава) go to ❔ unless a surname
settles it. Nicknames, emoji-only names and «Мама» will be wrong sometimes;
that is the cost of guessing, and why ❔ is a toggle rather than silently
dropped.

## ⚙️ Настройки

Stored in Postgres per account; defaults come from `config.toml`.
**Changes apply immediately, including to a campaign that is already
running** — the sender re-reads them before every message.

| Setting | Default | Notes |
| --- | --- | --- |
| 🏁 Дедлайн | 25.09.2026 21:00 | Sending stops when it passes; the unsent stay resumable |
| ⏱ Интервал | 180 s | Pause between two messages; floor 10 s |
| 🎲 Джиттер | ±35% | Each pause is randomised: 180 s ±35% = 117–243 s |
| 🌍 Часовой пояс | Europe/Moscow | Deadline and quiet hours are read in this zone |

The deadline is stored as local wall-clock time, so «21:00» keeps meaning 21:00
wherever you set the timezone.

**Why the timezone matters:** the Railway container runs in UTC. Before this
setting existed, quiet hours 23:00–10:00 and the 21:00 deadline were
silently applied in UTC — three hours off from Moscow.

### Why jitter

A message exactly every 180 s is a machine signature; people do not type on a
metronome. Jitter varies each gap so the account's rhythm looks like a person
working through a list. It does not raise throughput or get around any limit —
the average pace is the same. What keeps an account safe is the pace itself
and not being reported, which is why the defaults are slow.

## The launch screen

Before 🚀 the panel shows what the chosen interval means against the deadline:

```
👥 Получателей: 1400
⏱ Интервал: 3 мин ±35% (1 мин 57 с–4 мин 3 с)
📈 Темп: ~20/час, ~256/сутки
🌙 Ночью не шлём: 23:00–10:00 (Europe/Moscow)
🏁 Дедлайн: 25.09 21:00

⛔️ Не успеваем. До дедлайна уйдёт ~1143 из 1400, остальным ~257 — нет: в дедлайн рассылка остановится.
Чтобы успеть всем, нужен интервал ~2 мин 26 с — или сузь фильтр, или сдвинь дедлайн.
🟢 Темп спокойный.
```

(Rendered from the code for 1,400 people at 15:00 on 21.09.)

From 21.09 15:00 to a 25.09 21:00 deadline, this is how many people each
interval reaches:

| Интервал | Per day | Reached by deadline | |
| --- | --- | --- | --- |
| 60 s | ~746 | ~3,300 | 🔴 |
| 120 s | ~381 | ~1,700 | 🟡 |
| **180 s** | **~256** | **~1,140** | 🟢 |
| 300 s | ~155 | ~690 | 🟢 |

The colour is about the account, not the clock: a warmed-up account is
comfortable around 200–300/day, and limits tend to land above 600/day.

## Where the state lives

**Postgres.** Nothing that matters is in memory or on the container disk.

| Table | Holds |
| --- | --- |
| `recipients` | Dialogue scan: who, when you last spoke, guessed gender |
| `campaigns` | Text, filter, status, stop reason |
| `deliveries` | One row per recipient per campaign: `pending` → `sending` → `sent` / `skipped` / `failed` |
| `settings` | Deadline, interval, jitter, timezone |

Every delivery is committed as it happens. A redeploy loses at most the message
in flight, and that is requeued on the next boot; the panel then offers
**▶️ Продолжить**. Rows are scoped by `TG_ACCOUNT`.

The Telethon session lives in the `TG_SESSION` variable, because a `.session`
file on the container disk would be wiped by every deploy. Treat it like a
password.

## Deploying

Railway project `tg-message-sender-bot`: `Postgres` plus a `bot` service built
from the `Dockerfile`, with `DATABASE_URL=${{Postgres.DATABASE_URL}}`.

| Variable | Value |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | my.telegram.org |
| `TG_SESSION` | output of `python -m tgsender session` |
| `BOT_TOKEN` | @BotFather |
| `ADMIN_IDS` | numeric ids and/or `@usernames`, comma separated |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `TG_ACCOUNT` | `main` — one label per Telegram account, never reused |

The GitHub webhook has skipped a push before. After pushing, check the commit
on the Railway deployment; `railway up --service bot --detach` always works.

## Who can use the panel

`ADMIN_IDS=111111111, @nylinary, some_friend` — ids, usernames, or both. Usernames
are resolved to ids at startup and logged. Prefer ids: a username can be
released and claimed by someone else, and this panel sends from your account.
Anyone else is ignored silently.

## Safety rails

| Signal | Response |
| --- | --- |
| `FLOOD_WAIT_X` | Wait X + jitter, slow every later gap ×1.5 (up to ×8), notify admins |
| `FLOOD_WAIT` > 6 h | Stop — the account is restricted, not throttled |
| `PEER_FLOOD` | Stop — retrying is what makes a limit permanent |
| `USER_PRIVACY_RESTRICTED` | Skip that person, continue |
| Deadline passed | Stop before the next message; the rest stay resumable |

Always on: quiet hours 23:00–10:00 in your timezone, and a longer pause every
60 messages.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && $EDITOR .env
.venv/bin/python -m tgsender session      # once, to mint TG_SESSION
.venv/bin/python -m tgsender run
```

Never run the local copy and the deployed one at the same time: both would poll
the same bot token and both would try to send.

## Tests

```bash
.venv/bin/python tests/test_offline.py                   # logic, no network
DATABASE_URL=... .venv/bin/python tests/test_db.py       # Postgres layer
DATABASE_URL=... .venv/bin/python tests/test_wiring.py   # the panel, end to end
```

`test_wiring.py` drives the real dispatcher, admin gate and FSM with a fake
Telegram and a recording userbot: counting, filters, settings, launching,
resume, stale buttons, and the deadline stop. The database suites use
throwaway account scopes and clean up after themselves.

## Layout

| File | Role |
| --- | --- |
| `config.toml` | Defaults for settings, quiet hours, risk thresholds |
| `tgsender/bot.py` | The panel |
| `tgsender/filters.py` | Period presets and parsing; the `Filter` object |
| `tgsender/gender.py` | Gender guess from display names |
| `tgsender/settings.py` | Runtime settings and their parsing |
| `tgsender/scheduling.py` | Pace, finish time and deadline arithmetic |
| `tgsender/sender.py` | Send loop, error handling, backoff, deadline stop |
| `tgsender/db.py` | Postgres schema and every state transition |
