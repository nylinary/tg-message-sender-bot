# tgsender

A private Telegram bot that mails one invite, unchanged, to everyone you
already have a dialogue with — sent from your own account, paced to land by a
deadline you set.

Two Telegram identities, two different jobs:

- **The control bot** (`BOT_TOKEN`, made with @BotFather) is the panel you and
  your friends press buttons in. It is whitelisted to specific user IDs and
  never messages a guest.
- **The userbot** (`TG_API_ID` / `TG_API_HASH`, your personal account over
  MTProto) is what actually sends. Telegram bots cannot start a conversation
  with someone who has not written to them first, which is exactly why the
  sending has to come from your account.

## How it works

```
/start
  └─ 📝 Новая рассылка
       └─ you send the invite text          → saved as-is, formatting kept
            └─ pick a filter                → "last dialogue no older than 1 / 2 / 3 years / all"
                 └─ the panel computes      → recipients, interval, per-hour, per-day, risk
                      └─ 🚀 Запустить       → sends in the background
                           └─ 📊 Статус     → progress bar, rate, ETA, errors
                                ⏹ Остановить → stops; ▶️ Продолжить picks up where it left off
```

The message goes out **identical to everyone**. No names, no substitutions, no
tiers.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # api_id/api_hash, bot token, your user IDs
$EDITOR config.toml           # set the deadline
.venv/bin/python -m tgsender login   # authorise the sending account, once
.venv/bin/python -m tgsender run     # start the panel
```

`login` asks for phone, code, and 2FA. The session lands in `state/<account>/`
and **is** your account — it is gitignored, keep it that way.

Get your numeric user ID from [@userinfobot](https://t.me/userinfobot) and put
it in `ADMIN_IDS`, comma separated, along with the friends who should have
access. Anyone not on that list is ignored silently — no reply, no hint the bot
exists.

Keep `python -m tgsender run` alive for the whole campaign (`tmux`, `screen`, or
a systemd unit). If the process dies mid-run nothing is lost: the campaign is
marked stopped and the panel offers **▶️ Продолжить**, which re-sends to nobody
who already received it.

## The deadline drives the rate

Set `deadline` in `config.toml`. The panel divides the recipients by the
sendable time left — wall-clock minus quiet hours — and spaces the messages to
land exactly on it. It re-derives that interval every 25 messages, so a
`FLOOD_WAIT` or a manual pause gets absorbed instead of quietly overshooting.

For the current deadline (25.09 21:00) there are **63 sendable hours** inside
115 hours of wall clock, once nights are excluded:

| Recipients | Interval | Per hour | Per day | |
| --- | --- | --- | --- | --- |
| 500 | ~7 min | 8 | 104 | 🟢 |
| 1,000 | ~3 min | 16 | 209 | 🟢 |
| 1,500 | ~2 min | 24 | 313 | 🟡 |
| 2,000 | ~1 min | 32 | 417 | 🟡 |
| 3,000 | ~1 min | 48 | 626 | 🔴 |
| 4,000 | ~56 s | 63 | 835 | 🔴 |

**Every one of these fits the deadline.** The colour is not about the clock — it
is about the account. Empirically, a warmed-up account messaging existing
dialogues is comfortable around 200–300/day; 600+/day is where limits start
landing. The panel shows you the band and lets you launch anyway. It is your
account and your call.

If you are near the red, the lever that costs you nothing is the **filter**.
"Last dialogue within 1 year" is both a smaller number and a warmer audience —
people who have spoken to you recently are dramatically less likely to press
"Report spam", and reports are what actually gets accounts limited, far more
than raw throughput.

## Safety rails that are not negotiable

These exist because ignoring them turns a temporary limit into a permanent one:

| Signal | What it means | What the bot does |
| --- | --- | --- |
| `FLOOD_WAIT_X` | Routine throttling | Waits X + jitter, multiplies later intervals by 1.5× (capped at 8×), tells you |
| `FLOOD_WAIT` > 6h | The account is restricted, not throttled | Stops the campaign and notifies every admin |
| `PEER_FLOOD` | Flagged as a spammer | Stops immediately — retrying is what makes it permanent |
| `USER_PRIVACY_RESTRICTED` | That person's settings block you | Skips them, run continues |
| `USER_DEACTIVATED_BAN` | Account banned | Stops |

Also always on: quiet hours (nothing sends 23:00–10:00 local, because 3am
messages produce reports), a random ±35% jitter on every interval so the cadence
is not machine-flat, a longer pause every 60 messages, and an 8-second floor —
below that Telegram just answers with `FLOOD_WAIT` and you end up slower.

## Before the real run

Point `ADMIN_IDS` at yourself, pick the narrowest filter, and watch the first
20 or so land. Then open [@SpamBot](https://t.me/SpamBot) from the sending
account and check it says no limits apply. That costs you twenty minutes and
tells you whether the pace is survivable before you have spent 2,000 messages
finding out.

## Layout

| File | Role |
| --- | --- |
| `config.toml` | Deadline, filter options, pacing, risk thresholds |
| `tgsender/scheduling.py` | Deadline → interval maths, risk bands |
| `tgsender/bot.py` | The panel: keyboards, compose flow, status |
| `tgsender/sender.py` | Send loop, error classification, backoff |
| `tgsender/collect.py` | Dialogue scan |
| `tgsender/db.py` | SQLite — campaigns, per-recipient delivery state |
| `tests/` | Offline tests; no network needed |

```bash
.venv/bin/python tests/test_offline.py
.venv/bin/python tests/test_wiring.py
```

## Notes

- **One campaign at a time.** The panel refuses to start a second.
- **Nobody is invited twice.** A new campaign automatically excludes anyone who
  received an earlier one, and says how many it excluded. Pass
  `exclude_sent=False` to `create_campaign` if you ever genuinely want a
  re-send.
- **Text only.** Photos and files are rejected at compose time. Formatting and
  links survive; the bot verifies Telethon can parse the markup before
  accepting it, rather than discovering it mid-campaign.
- **The panel is shared, the account is not.** Any whitelisted friend can start
  a campaign, and it sends from *your* account.
- Everything the panel says is in Russian; the strings are inline in
  `tgsender/bot.py`.
