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

The message goes out **identical to everyone**. No names, no substitutions.

## Where the state lives

**Postgres.** Nothing that matters is held in memory or on the container disk.

| Table | Holds |
| --- | --- |
| `recipients` | The cached dialogue scan — who exists, when you last spoke |
| `campaigns` | One row per send: the text, the filter, status, pace, stop reason |
| `deliveries` | **One row per recipient per campaign** — `pending` → `sending` → `sent` / `skipped` / `failed` |

Every delivery is committed the instant it happens. A redeploy, crash, or
`railway restart` loses **at most the single message in flight**, and even that
is recovered: anything left `sending` is returned to `pending` on the next boot.

On startup the bot finds any campaign the database still calls `running`,
marks it `stopped` honestly, requeues the in-flight row, and messages every
admin that it restarted. The panel then shows **▶️ Продолжить**, which sends
only to people who have not yet received it. **Redeploy mid-campaign is safe.**

Rows are scoped by `TG_ACCOUNT`, so several sending accounts can share one
database without seeing each other's recipients or already-sent guards.

### The other thing that must not be ephemeral

The Telethon session — your account's authorization — normally lives in a
`.session` file. On Railway that file is wiped by every deploy, and restoring
it needs a login code that nobody can type inside a container.

So the session is carried in **`TG_SESSION`**, a string you generate once:

```bash
python -m tgsender session     # log in, prints the string
```

Put it in Railway's variables. Treat it exactly like a password — it is full
read and write access to your Telegram account. It is in `.gitignore` and must
never be committed.

## Deploying

Already provisioned on Railway (project `tg-message-sender-bot`): a `Postgres`
service and a `bot` service built from the `Dockerfile`, with
`DATABASE_URL` referencing `${{Postgres.DATABASE_URL}}` over the private
network.

Variables the `bot` service needs:

| Variable | Value |
| --- | --- |
| `TG_API_ID`, `TG_API_HASH` | from my.telegram.org |
| `TG_SESSION` | output of `python -m tgsender session` |
| `BOT_TOKEN` | from @BotFather |
| `ADMIN_IDS` | numeric ids and/or `@usernames`, comma separated — see below |
| `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` |
| `TG_ACCOUNT` | `main` |

Deploys happen on push to `main`. There is no HTTP port — this is a worker, so
Railway showing no domain is correct.

## Who can use the panel

`ADMIN_IDS` takes numeric ids, `@usernames`, or both, mixed:

```
ADMIN_IDS=111111111, @nylinary, some_friend
```

The `@` is optional and case is ignored. Get numeric ids from
[@userinfobot](https://t.me/userinfobot).

At startup the bot resolves every username to a numeric id through the sending
account and logs the mapping, so the running filter compares ids, not names.
If a handle cannot be resolved — the person is not reachable from that account,
or Telegram is being difficult — the bot logs a warning and falls back to
matching that one by name, pinning their id the first time they press a button.

**Prefer numeric ids where you have them.** A username can be released and
re-registered by anyone, and this panel sends from *your* personal account. An
id is permanent. Usernames are here for convenience, not as the security
boundary.

Anyone not on the list is ignored silently — no reply, no hint the bot exists.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && $EDITOR .env      # needs a DATABASE_URL too
.venv/bin/python -m tgsender session      # once, to mint TG_SESSION
.venv/bin/python -m tgsender run
```

Point `DATABASE_URL` at the Railway Postgres public proxy, or any local
instance. Do not run the local copy and the deployed one against the same
database at the same time — both would try to send.

## The deadline drives the rate

Set `deadline` in `config.toml`. The panel divides the recipients by the
sendable time left — wall clock minus quiet hours — and spaces the messages to
land on it. It re-derives that interval every 25 messages, so a `FLOOD_WAIT` or
a pause gets absorbed instead of quietly overshooting.

For a 25.09 21:00 deadline there are **63 sendable hours** inside 115 hours of
wall clock, once nights are excluded:

| Recipients | Interval | Per hour | Per day | |
| --- | --- | --- | --- | --- |
| 500 | ~7 min | 8 | 104 | 🟢 |
| 1,000 | ~3 min | 16 | 209 | 🟢 |
| 1,500 | ~2 min | 24 | 313 | 🟡 |
| 2,000 | ~1 min | 32 | 417 | 🟡 |
| 3,000 | ~1 min | 48 | 626 | 🔴 |
| 4,000 | ~56 s | 63 | 835 | 🔴 |

**Every one of these fits the deadline.** The colour is not about the clock — it
is about the account. Empirically a warmed-up account messaging existing
dialogues is comfortable around 200–300/day; 600+/day is where limits land. The
panel shows the band and lets you launch anyway.

If you are near the red, the free lever is the **filter**. "Within 1 year" is
both a smaller number and a warmer audience, and spam reports — not raw
throughput — are what actually gets accounts limited.

## Safety rails

| Signal | Meaning | Response |
| --- | --- | --- |
| `FLOOD_WAIT_X` | Routine throttling | Wait X + jitter, multiply later intervals ×1.5 (capped ×8), notify |
| `FLOOD_WAIT` > 6h | Account restricted, not throttled | Stop, notify admins |
| `PEER_FLOOD` | Flagged as a spammer | Stop — retrying is what makes it permanent |
| `USER_PRIVACY_RESTRICTED` | Their settings block you | Skip, continue |
| `USER_DEACTIVATED_BAN` | Account banned | Stop |

Always on: quiet hours 23:00–10:00 local, ±35% jitter on every interval, a
longer pause every 60 messages, and an 8-second floor — below that Telegram
answers with `FLOOD_WAIT` and you end up slower.

## Tests

```bash
.venv/bin/python tests/test_offline.py                    # pure logic, no network
DATABASE_URL=... .venv/bin/python tests/test_db.py        # Postgres layer
DATABASE_URL=... .venv/bin/python tests/test_wiring.py    # aiogram wiring
```

The two database suites write under throwaway account scopes and clean up
after themselves; they skip silently without `DATABASE_URL`.

## Layout

| File | Role |
| --- | --- |
| `config.toml` | Deadline, filter options, pacing, risk thresholds |
| `tgsender/scheduling.py` | Deadline → interval maths, risk bands |
| `tgsender/bot.py` | The panel: keyboards, compose flow, status |
| `tgsender/sender.py` | Send loop, error classification, backoff |
| `tgsender/collect.py` | Dialogue scan |
| `tgsender/db.py` | Postgres schema and every state transition |
| `Dockerfile`, `railway.json` | Deploy |

## Notes

- **One campaign at a time.** The panel refuses to start a second.
- **Nobody is invited twice.** A new campaign excludes anyone who received an
  earlier one and says how many it excluded. `exclude_sent=False` on
  `create_campaign` overrides it if you ever want a genuine re-send.
- **Text only.** Photos and files are rejected at compose time. Formatting
  survives; the bot verifies Telethon can parse the markup before accepting it
  rather than discovering it mid-campaign.
- **The panel is shared, the account is not.** Any whitelisted friend can start
  a campaign, and it sends from *your* account.
- Panel strings are Russian, inline in `tgsender/bot.py`.
