"""Live currency prices from poe.ninja (PoE 1 economy API).

Endpoints used (documented at https://poe.ninja/docs/api):
  GET https://poe.ninja/poe1/api/economy/leagues
      -> [{"id": "Allflame", "name": "Allflame"}, ...]   first entry = current challenge league
  GET https://poe.ninja/poe1/api/economy/exchange/current/overview?league={id}&type={type}
      -> {"core":  {"primary": "chaos", "secondary": "divine",
                    "rates": {"divine": 0.0029},          # units of that currency per 1 primary
                    "items": [...]},                       # only the reference currencies
          "lines": [{"id": "alt", "primaryValue": 0.15, "volumePrimaryValue": ..., ...}],
          "items": [{"id": "alt", "name": "Orb of Alteration", "category": "Currency", ...}]}
  Names come from the TOP-LEVEL "items" array; core.items only lists primary/secondary.

Last verified against the live API: 2026-10-26 (league "Allflame", primary "chaos",
Cache-Control "public, max-age=1800, stale-if-error=86400", weak ETag, 304 on If-None-Match).
The old /api/data/currencyoverview endpoint is stale and not used.

Usage rules followed: descriptive User-Agent (optional contact via POECRAFT_CONTACT), on-disk
cache that honours max-age and never re-requests within MIN_INTERVAL, conditional requests with
the stored ETag, strictly sequential requests behind a process-wide lock.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from .. import __version__, config

API = os.environ.get("POECRAFT_NINJA_URL", "https://poe.ninja/poe1/api/economy").rstrip("/")
CATEGORIES = ["Currency", "Fragment", "Fossil", "Resonator", "Essence"]
MIN_INTERVAL = 300        # never hit the same URL more often than every 5 minutes
MAX_CACHE = 1800          # cap on how long a response is trusted without revalidation
LEAGUES_TTL = 3600
TIMEOUT = 10
SNAPSHOT_MAX_AGE = 86400  # matches poe.ninja's stale-if-error window
REQUEST_GAP = 0.5         # pause between consecutive network requests

_lock = threading.Lock()


class NinjaError(Exception):
    pass


def user_agent() -> str:
    return config.user_agent()          # one User-Agent (with the operator's contact) for every outgoing request


def _now_iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ttl(cache_control: str | None, default: int) -> int:
    m = re.search(r"max-age=(\d+)", cache_control or "")
    ttl = int(m.group(1)) if m else default
    return max(MIN_INTERVAL, min(ttl, MAX_CACHE))


def _get_json(url: str, key: str, default_ttl: int):
    """GET with on-disk cache + ETag revalidation. Returns (body, served_from_cache)."""
    path = config.NINJA_CACHE_DIR / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")
    entry = _read_json(path)
    if entry and entry.get("url") != url:
        entry = None  # legacy cache keys may collide; never trust another request
    now = time.time()
    if entry and now - entry.get("checked_at", 0) < entry.get("ttl", MIN_INTERVAL):
        return entry["body"], True
    headers = {"User-Agent": user_agent(), "Accept": "application/json"}
    if entry and entry.get("etag"):
        headers["If-None-Match"] = entry["etag"]
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            etag = resp.headers.get("ETag")
            ttl = _ttl(resp.headers.get("Cache-Control"), default_ttl)
    except urllib.error.HTTPError as e:
        if e.code == 304 and entry:
            entry["checked_at"] = now
            _write_json(path, entry)
            return entry["body"], True
        raise NinjaError(f"poe.ninja returned HTTP {e.code} for {url}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise NinjaError(f"cannot reach poe.ninja ({reason})") from None
    except ValueError:
        raise NinjaError(f"poe.ninja sent invalid JSON for {url}") from None
    finally:
        time.sleep(REQUEST_GAP)
    _write_json(path, {"url": url, "etag": etag, "checked_at": now, "ttl": ttl, "body": body})
    return body, False


def resolve_league(requested: str | None = None) -> str:
    leagues, _ = _get_json(f"{API}/leagues", "leagues", LEAGUES_TTL)
    ids = [x["id"] for x in leagues if isinstance(x, dict) and x.get("id")] if isinstance(leagues, list) else []
    if not ids:
        raise NinjaError("poe.ninja returned an empty league list")
    if requested:
        match = next((i for i in ids if i.lower() == requested.lower()), None)
        if match is None:
            raise NinjaError(f"unknown league '{requested}' (poe.ninja lists: {', '.join(ids)})")
        return match
    return ids[0]


def parse_overview(body: dict) -> dict[str, float]:
    """Exchange overview -> {in-game item name: price in chaos}."""
    if not isinstance(body, dict):
        raise NinjaError("unexpected overview response")
    core = body.get("core") or {}
    primary, rates = core.get("primary"), core.get("rates") or {}
    if primary == "chaos":
        factor = 1.0
    elif isinstance(rates.get("chaos"), (int, float)) and rates["chaos"] > 0:
        factor = float(rates["chaos"])   # chaos per 1 primary
    else:
        raise NinjaError(f"overview quoted in '{primary}' with no chaos rate")
    names = {}
    for it in (body.get("items") or []) + (core.get("items") or []):
        if isinstance(it, dict) and it.get("id") and it.get("name"):
            names.setdefault(it["id"], it["name"])
    out = {}
    for line in body.get("lines") or []:
        name, value = names.get(line.get("id")), line.get("primaryValue")
        if name and isinstance(value, (int, float)) and value > 0:
            out[name] = round(value * factor, 6)
    return out


def update(league: str | None = None, path: Path | None = None) -> dict:
    """Fetch (or reuse cached) prices for CATEGORIES and write the snapshot. Never raises."""
    path = path or config.LIVE_PRICES_PATH
    with _lock:
        snap = _read_json(path) or {}
        meta = snap.get("_meta", {})
        requested = league or os.environ.get("POECRAFT_LEAGUE") or None
        attempt = {"selection_verified": False, "requested_league": requested, "at": _now_iso(), "ok": False, "error": None}
        try:
            lg = resolve_league(league or os.environ.get("POECRAFT_LEAGUE") or None)
        except NinjaError as e:
            attempt["error"] = str(e)
            snap["_meta"] = {**meta, "last_attempt": attempt}
            _write_json(path, snap)
            return load_status(path)
        attempt["requested_league"] = lg
        attempt["selection_verified"] = True
        prices, cats = {}, {}
        for t in CATEGORIES:
            url = f"{API}/exchange/current/overview?league={quote(lg)}&type={quote(t)}"
            try:
                body, cached = _get_json(url, f"{lg}_{t}_{hashlib.sha256(url.encode()).hexdigest()[:16]}", MIN_INTERVAL)
                p = parse_overview(body)
                prices.update(p)
                cats[t] = {"ok": True, "count": len(p), "cached": cached}
            except NinjaError as e:
                cats[t] = {"ok": False, "error": str(e)}
        failed = [t for t, c in cats.items() if not c["ok"]]
        attempt["ok"] = bool(prices)
        attempt["error"] = "; ".join(f"{t}: {cats[t]['error']}" for t in failed) or None
        if prices:
            snap = {"_meta": {"source": API, "league": lg, "fetched_at": _now_iso(), "categories": cats,
                              "last_attempt": attempt}, "prices": prices}
        else:
            snap["_meta"] = {**meta, "last_attempt": attempt}
        _write_json(path, snap)
        return load_status(path)


def load_status(path: Path | None = None, league: str | None = None) -> dict:
    """Interpret the snapshot: {'state': live|stale|unavailable, 'prices': {...}, 'message': ...}."""
    path = path or config.LIVE_PRICES_PATH
    snap = _read_json(path)
    fallback = "using estimates from config/prices.json"
    if not snap or not snap.get("prices"):
        err = ((snap or {}).get("_meta", {}).get("last_attempt") or {}).get("error")
        msg = f"Live prices unavailable ({err or 'never fetched'}) - {fallback}."
        return {"state": "unavailable", "prices": {}, "message": msg, "league": None, "fetched_at": None,
                "message_key": "m.price.unavailable" if err else "m.price.unavailable_never",
                "message_params": {"error": err} if err else {}}
    meta = snap.get("_meta", {})
    attempt = meta.get("last_attempt") or {}
    if (attempt.get("selection_verified") is not True or meta.get("source") != API):
        return {"state": "unavailable", "prices": {}, "league": attempt.get("requested_league"),
                "fetched_at": None, "message": "League selection could not be verified; using estimates.",
                "message_key": "m.price.league_unverified", "message_params": {}}
    requested = league or os.environ.get("POECRAFT_LEAGUE") or attempt.get("requested_league")
    if requested and requested.casefold() != str(meta.get("league", "")).casefold():
        return {"state": "unavailable", "prices": {}, "league": requested, "fetched_at": None,
                "message": f"Snapshot league {meta.get('league')} differs from requested {requested}; using estimates.",
                "message_key": "m.price.league_mismatch",
                "message_params": {"actual": meta.get("league"), "requested": requested}}
    fetched = meta.get("fetched_at")
    try:
        age = time.time() - calendar.timegm(time.strptime(fetched, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        age = float("inf")
    attempt = meta.get("last_attempt") or {}
    base = {"league": meta.get("league"), "fetched_at": fetched, "categories": meta.get("categories", {})}
    if age > SNAPSHOT_MAX_AGE:
        return {**base, "state": "unavailable", "prices": {},
                "message": f"Live prices unavailable (last snapshot {fetched} is older than 24 h"
                           f"{'; ' + attempt['error'] if attempt.get('error') else ''}) - {fallback}.",
                "message_key": "m.price.too_old",
                "message_params": {"fetched": fetched, "error": "; " + attempt["error"] if attempt.get("error") else ""}}
    if not attempt.get("ok", True):
        return {**base, "state": "stale", "prices": snap["prices"],
                "message": f"poe.ninja unreachable at {attempt.get('at')} ({attempt.get('error')}); "
                           f"using the last live snapshot from {fetched} ({meta.get('league')}).",
                "message_key": "m.price.unreachable",
                "message_params": {"at": attempt.get("at"), "error": attempt.get("error"), "fetched": fetched,
                                   "league": meta.get("league")}}
    msg = None
    if attempt.get("error"):
        msg = (f"Some poe.ninja categories failed ({attempt['error']}); those items use "
               f"estimates from config/prices.json.")
    # message_key / message_params: the same message as a locale key, so UI and plans can show it translated
    return {**base, "state": "live", "prices": snap["prices"], "message": msg,
            **({"message_key": "m.price.partial", "message_params": {"error": attempt["error"]}} if msg else {})}
