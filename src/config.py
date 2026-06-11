"""Central configuration, loaded from .env. No secrets are hardcoded."""
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(PROJECT_ROOT / ".env")


def _ids(key: str) -> set[int]:
    raw = os.environ.get(key, "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().isdigit()}


def _json_env(key: str, default):
    raw = os.environ.get(key, "")
    if not raw.strip():
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


# --- Telegram ---
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_ID = int(os.environ.get("API_ID") or 0)
API_HASH = os.environ.get("API_HASH", "")
PREMIUM_SESSION = os.environ.get("PREMIUM_SESSION", "")
# Optional local telegram-bot-api server for uploads up to 2GB. When set, files <=2GB are sent
# through it (HTTP), which resolves the recipient natively (no MTProto peer/access_hash issue
# the Pyrogram bot client hits). >2GB still goes through the Premium user client.
BOT_API_BASE = os.environ.get("UPLOAD_BOT_API_BASE", "").rstrip("/")
# Dump channel for >2GB relay: the Premium user client (MTProto, no 2GB cap) uploads the big
# file here, then the bot copies it to the recipient via the Bot API (which resolves the user
# natively - no PEER_ID_INVALID). Requires BOTH the Premium account and the bot to be members.
DUMP_CHANNEL = int(os.environ.get("UPLOAD_DUMP_CHANNEL") or 0)
ADMIN_IDS = _ids("ADMIN_IDS")

# --- unshackle REST API ---
UNSHACKLE_API = os.environ.get("UNSHACKLE_API", "http://127.0.0.1:8786/api").rstrip("/")
UNSHACKLE_API_KEY = os.environ.get("UNSHACKLE_API_KEY", "")

# --- Service catalog routing (data-driven; the framework ships generic, a deployment
# configures these for its own services - no service names are hardcoded in the code) ---
# {tag: "il"|"free"|"sub"} category seeds (admin overrides in categories.json win over these)
CATEGORY_SEEDS = _json_env("CATEGORY_SEEDS", {})
# {domain_substring: tag} fast URL routing (otherwise each service's title_regex/url is scanned)
DOMAIN_SERVICES = _json_env("DOMAIN_SERVICES", {})
# tags that are free even though they expose an optional login (e.g. cookies unlock extras)
FREE_SERVICES = set(_json_env("FREE_SERVICES", []))
# service tag that handles any otherwise-unmatched http link (e.g. a yt-dlp service); "" = none
CATCHALL_SERVICE = os.environ.get("CATCHALL_SERVICE", "")
# service tag that handles podcast/RSS feed URLs; "" = none
FEED_SERVICE = os.environ.get("FEED_SERVICE", "")

# --- Monitors ---
# IANA timezone for fixed-time monitor schedules (e.g. "Asia/Jerusalem"). Default UTC.
SCHEDULE_TZ = os.environ.get("SCHEDULE_TZ", "UTC")

# --- Paths ---
# where per-user .wvd CDM devices live - MUST equal unshackle.yaml directories.wvds
WVD_DIR = Path(os.environ.get("WVD_DIR", PROJECT_ROOT.parent / "unshackle-data" / "WVDs"))
COOKIES_DIR = Path(os.environ.get("COOKIES_DIR", PROJECT_ROOT.parent / "unshackle-data" / "cookies"))
DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", PROJECT_ROOT.parent / "unshackle-data" / "downloads"))
STATE_DIR = Path(os.environ.get("STATE_DIR", PROJECT_ROOT / "state"))
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY", "")


def is_admin(tg_id: int) -> bool:
    return tg_id in ADMIN_IDS


def ensure_dirs() -> None:
    for d in (COOKIES_DIR, DOWNLOADS_DIR, STATE_DIR, STATE_DIR / "queue", STATE_DIR / "jobs"):
        d.mkdir(parents=True, exist_ok=True)
