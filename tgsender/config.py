from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Pacing:
    interval: float
    jitter: float
    min_interval: float
    quiet_start: int
    quiet_end: int
    night_mode: str
    long_pause_every: int
    long_pause_min: float
    long_pause_max: float
    flood_backoff: float
    flood_backoff_max: float


@dataclass(frozen=True)
class Risk:
    warn_per_day: int
    danger_per_day: int


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admin_ids: frozenset[int]
    admin_usernames: frozenset[str]
    account: str
    deadline: datetime      # naive, local to `timezone`
    timezone: str
    pacing: Pacing
    risk: Risk
    stale_after_hours: float
    database_url: str
    session_string: str | None
    session_path: Path


HINTS = {
    "TG_API_ID": "from https://my.telegram.org -> API development tools",
    "TG_API_HASH": "from https://my.telegram.org -> API development tools",
    "BOT_TOKEN": "from @BotFather",
    "ADMIN_IDS": "numeric ids (ask @userinfobot) and/or @usernames, comma separated",
}


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        where = (
            "Set it in the Railway service variables."
            if os.environ.get("RAILWAY_ENVIRONMENT")
            else "Copy .env.example to .env and fill it in."
        )
        hint = HINTS.get(name)
        raise SystemExit(
            f"{name} is not set. {where}" + (f"\n  ({hint})" if hint else "")
        )
    return value


# Telegram usernames: 5-32 chars, must start with a letter, letters/digits/_ only.
USERNAME_RE = re.compile(r"[a-z][a-z0-9_]{3,31}")


def parse_admins(raw: str) -> tuple[frozenset[int], frozenset[str]]:
    """Split ADMIN_IDS into numeric ids and @usernames.

    Both forms are accepted, mixed freely:
        ADMIN_IDS=111111111, @nylinary, someone_else

    Usernames are a convenience: they are resolved to numeric ids at startup,
    because a username can be released and re-registered by somebody else,
    while an id is permanent.
    """
    ids: set[int] = set()
    usernames: set[str] = set()

    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        if token.lstrip("-").isdigit():
            ids.add(int(token))
            continue
        name = token.lstrip("@").lower()
        if not USERNAME_RE.fullmatch(name):
            raise SystemExit(
                f"ADMIN_IDS: {token!r} is neither a numeric id nor a valid "
                f"Telegram username (5-32 chars, starts with a letter, "
                f"letters/digits/underscore only)."
            )
        usernames.add(name)

    if not ids and not usernames:
        raise SystemExit("ADMIN_IDS is empty — nobody would be able to use the panel.")
    return frozenset(ids), frozenset(usernames)


def require_database_url(cfg: "Config") -> str:
    if not cfg.database_url:
        raise SystemExit(
            "DATABASE_URL is not set.\n"
            "On Railway, reference the Postgres service: ${{Postgres.DATABASE_URL}}\n"
            "Locally, point it at any Postgres instance."
        )
    return cfg.database_url


def normalize_dsn(url: str) -> str:
    """asyncpg rejects the SQLAlchemy-style schemes some hosts hand out."""
    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql://"),
        ("postgresql+psycopg2://", "postgresql://"),
        ("postgres://", "postgresql://"),
    ):
        if url.startswith(prefix):
            return replacement + url[len(prefix) :]
    return url


def load(config_path: Path | None = None) -> Config:
    path = config_path or ROOT / "config.toml"
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    admin_ids, admin_usernames = parse_admins(_require("ADMIN_IDS"))
    account = os.environ.get("TG_ACCOUNT", "main").strip() or "main"
    state_dir = ROOT / raw["files"]["state_dir"] / account
    state_dir.mkdir(parents=True, exist_ok=True)

    # A session string survives a redeploy; the .session file on disk does not.
    # Local development can still use the file, but anything deployed must set
    # TG_SESSION or it will ask for a login code nobody can type.
    session_string = os.environ.get("TG_SESSION", "").strip() or None

    return Config(
        api_id=int(_require("TG_API_ID")),
        api_hash=_require("TG_API_HASH"),
        bot_token=_require("BOT_TOKEN"),
        admin_ids=admin_ids,
        admin_usernames=admin_usernames,
        account=account,
        deadline=datetime.fromisoformat(raw["campaign"]["deadline"]).replace(tzinfo=None),
        timezone=raw["campaign"].get("timezone", "Europe/Moscow"),
        pacing=Pacing(**raw["pacing"]),
        risk=Risk(**raw["risk"]),
        stale_after_hours=float(raw["collect"]["stale_after_hours"]),
        # Not required here: `session` runs before any database exists.
        # `run` validates it via require_database_url().
        database_url=normalize_dsn(os.environ.get("DATABASE_URL", "").strip()),
        session_string=session_string,
        session_path=state_dir / f"{account}.session",
    )
