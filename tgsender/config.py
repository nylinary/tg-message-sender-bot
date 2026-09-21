from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Pacing:
    quiet_start: int
    quiet_end: int
    min_delay: float
    jitter: float
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
    account: str
    deadline: datetime
    age_options: tuple[int, ...]
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
    "ADMIN_IDS": "your numeric Telegram id (ask @userinfobot), comma separated",
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


def _admin_ids() -> frozenset[int]:
    raw = _require("ADMIN_IDS")
    try:
        ids = {int(part) for part in raw.replace(" ", "").split(",") if part}
    except ValueError as exc:
        raise SystemExit(f"ADMIN_IDS must be comma-separated numbers: {exc}") from exc
    if not ids:
        raise SystemExit("ADMIN_IDS is empty — nobody would be able to use the panel.")
    return frozenset(ids)


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
        admin_ids=_admin_ids(),
        account=account,
        deadline=datetime.fromisoformat(raw["campaign"]["deadline"]),
        age_options=tuple(raw["campaign"]["age_options"]),
        pacing=Pacing(**raw["pacing"]),
        risk=Risk(**raw["risk"]),
        stale_after_hours=float(raw["collect"]["stale_after_hours"]),
        # Not required here: `session` runs before any database exists.
        # `run` validates it via require_database_url().
        database_url=normalize_dsn(os.environ.get("DATABASE_URL", "").strip()),
        session_string=session_string,
        session_path=state_dir / f"{account}.session",
    )
