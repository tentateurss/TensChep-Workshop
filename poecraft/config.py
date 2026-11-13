"""Paths and data-source settings. Everything league-dependent lives in data/ and config/."""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent

DATA_DIR = Path(os.environ.get("POECRAFT_DATA_DIR", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "poecraft.db"
MANIFEST_PATH = RAW_DIR / "manifest.json"
LIVE_PRICES_PATH = DATA_DIR / "prices_live.json"     # written by data/ninja.py
NINJA_CACHE_DIR = DATA_DIR / "ninja_cache"           # raw poe.ninja responses + ETags

CONFIG_DIR = Path(os.environ.get("POECRAFT_CONFIG_DIR", ROOT / "config"))
PRICES_PATH = CONFIG_DIR / "prices.json"

RULES_PATH = PACKAGE_DIR / "rules" / "mechanics.json"
WEB_DIR = PACKAGE_DIR / "web"

# RePoE fork: community export of the PoE 1 game files (GGPK) to JSON, rebuilt every patch.
# Free, no API key. Override with POECRAFT_REPOE_URL to point at a mirror or a local copy.
REPOE_BASE_URL = os.environ.get("POECRAFT_REPOE_URL", "https://repoe-fork.github.io/")
REPOE_FILES = [
    "mods",
    "base_items",
    "essences",
    "fossils",
    "crafting_bench_options",
    "item_classes",
]
# Game-data localization (data/locale_game_<code>.json): language code -> RePoE export folder. Adding a language
# here (plus a UI file in poecraft/locales/) makes `update` fetch and validate it; see data/locales.py.
LOCALE_LANGUAGES = {"ru": "Russian", "de": "German"}

# Item classes the planner models (equipment with 3 prefix / 3 suffix slots).
SUPPORTED_ITEM_CLASSES = [
    "Claw", "Dagger", "Rune Dagger", "Wand", "One Hand Sword", "Thrusting One Hand Sword",
    "One Hand Axe", "One Hand Mace", "Sceptre", "Bow", "Staff", "Warstaff",
    "Two Hand Sword", "Two Hand Axe", "Two Hand Mace",
    "Quiver", "Belt", "Gloves", "Boots", "Body Armour", "Helmet", "Shield", "Amulet", "Ring",
]

CURRENCY_CLASSES = {"StackableCurrency", "DelveSocketableCurrency", "DelveStackableSocketableCurrency"}

DEFAULT_SIMULATIONS = int(os.environ.get("POECRAFT_SIMS", "2000"))
# Wall-clock limits for the simulation check only (the fast calculation is never cut short). A strategy whose
# simulation runs past SIM_TIME_LIMIT_S stops there and is flagged low-confidence (timeout); once the whole
# simulation phase has used SIM_PHASE_LIMIT_S, the remaining strategies are not simulated at all.
SIM_TIME_LIMIT_S = float(os.environ.get("POECRAFT_SIM_TIME_LIMIT", "5"))
SIM_PHASE_LIMIT_S = float(os.environ.get("POECRAFT_SIM_PHASE_LIMIT", "20"))
# The optional "recovery via Annul / metacraft" variant solves a policy over item states (part of the fast
# calculation). With many targets that can take minutes, so each solve gets RECOVERY_TIME_LIMIT_S and all of a
# plan's solves together RECOVERY_PHASE_LIMIT_S; past that the variant is skipped (noted), exactly as when it has
# too many item states. Plans whose solves finish in time are unchanged.
RECOVERY_TIME_LIMIT_S = float(os.environ.get("POECRAFT_RECOVERY_TIME_LIMIT", "5"))
RECOVERY_PHASE_LIMIT_S = float(os.environ.get("POECRAFT_RECOVERY_PHASE_LIMIT", "15"))


# ---------------------------------------------------------------- hosting (python -m poecraft serve --production)
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_rate(name: str, default: str) -> tuple[float, float]:
    """'10/20' -> (10 requests per second, burst of 20)."""
    raw = os.environ.get(name, default)
    try:
        rate, burst = raw.split("/")
        return float(rate), float(burst)
    except ValueError:
        rate, burst = default.split("/")
        return float(rate), float(burst)


PRODUCTION = os.environ.get("POECRAFT_ENV", "").lower() == "production"
HOST = os.environ.get("POECRAFT_HOST", "0.0.0.0" if PRODUCTION else "127.0.0.1")
PORT = _env_int("POECRAFT_PORT", 8765)
# Contact for the User-Agent of every outgoing request (poe.ninja, pathofexile.com, RePoE, OCR.space): GGG asks API
# users to identify themselves. Set it to the operator's address - it is never filled in automatically.
CONTACT = os.environ.get("POECRAFT_CONTACT", "").strip()
USER_AGENT_OVERRIDE = os.environ.get("POECRAFT_USER_AGENT", "").strip()
CORS_ORIGINS = [o.strip() for o in os.environ.get("POECRAFT_CORS_ORIGINS", "").split(",") if o.strip()]
TRUST_PROXY = os.environ.get("POECRAFT_TRUST_PROXY", "") in ("1", "true", "yes")   # use X-Forwarded-For (behind Caddy)
MAX_BODY = _env_int("POECRAFT_MAX_BODY", 2 * 1024 * 1024)      # a screenshot for OCR is the largest request
MAX_CONNECTIONS = _env_int("POECRAFT_MAX_CONNECTIONS", 64)     # handler threads at once; more get 503
SOCKET_TIMEOUT = _env_int("POECRAFT_SOCKET_TIMEOUT", 30)       # seconds a client may stall a connection
PLAN_QUEUE_WAIT = _env_int("POECRAFT_PLAN_QUEUE_WAIT", 60)     # max wait for the (single) planner; then 503
PRICE_REFRESH_MIN = _env_int("POECRAFT_PRICE_REFRESH_MIN", 30)  # production: background poe.ninja refresh
# per-IP limits: "requests per second/burst"
RATE_API = _env_rate("POECRAFT_RATE_API", "10/20")             # cheap API calls (meta, bases, families, search ...)
RATE_PLAN = _env_rate("POECRAFT_RATE_PLAN", "0.2/3")           # /api/plan, /api/recover: CPU heavy
RATE_TRADE = _env_rate("POECRAFT_RATE_TRADE", "1/5")           # /api/trade (also cached and limited towards GGG)
RATE_OCR = _env_rate("POECRAFT_RATE_OCR", "0.02/3")            # /api/ocr: uses the operator's OCR.space quota
# requests to the trade site's search endpoint (shared by all users): cache identical searches, cap the rate
TRADE_CACHE_TTL = _env_int("POECRAFT_TRADE_CACHE_TTL", 300)
TRADE_MAX_PER_10S = _env_int("POECRAFT_TRADE_MAX_PER_10S", 3)
TRADE_MAX_SEARCH_PER_5MIN = _env_int("POECRAFT_TRADE_MAX_SEARCH_PER_5MIN", 25)   # GGG allows 30 per 5 min per IP
TRADE_MAX_SEARCH_PER_MIN = _env_int("POECRAFT_TRADE_MAX_SEARCH_PER_MIN", 12)   # GGG allows 15 per 60 s per IP
EVAL_WAIT_BUDGET = _env_int("POECRAFT_EVAL_WAIT_BUDGET", 45)   # seconds one evaluation may wait for request slots
EVAL_MAX_SEARCHES = _env_int("POECRAFT_EVAL_MAX_SEARCHES", 5)    # trade searches one evaluation may use
RATE_EVAL = _env_rate("POECRAFT_RATE_EVAL", "0.1/3")           # /api/evaluate per client (each uses several searches)


def user_agent() -> str:
    """User-Agent for every outgoing request (POECRAFT_USER_AGENT replaces it entirely)."""
    if USER_AGENT_OVERRIDE:
        return USER_AGENT_OVERRIDE
    from . import __version__
    extra = f"; contact: {CONTACT}" if CONTACT else ""
    return f"poecraft-planner/{__version__} (Path of Exile 1 crafting planner; Python urllib{extra})"
