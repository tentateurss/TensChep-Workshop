"""Search links for the official Path of Exile trade site (pathofexile.com/trade), standard library only.

Data used (fetched from the trade site itself, cached on disk):
  GET  https://www.pathofexile.com/api/trade/data/leagues -> {"result": [{"id": "Allflame", "realm": "pc"}, ...]}
  GET  https://www.pathofexile.com/api/trade/data/stats   -> {"result": [{"id": "explicit", "entries":
                                                              [{"id": "explicit.stat_3299347043", "text": "+# to maximum Life"}]}]}
  POST https://www.pathofexile.com/api/trade/search/{league} with {"query": ..., "sort": ...} -> {"id": "OQMzdqQUE", ...}
       and the link is https://www.pathofexile.com/trade/search/{league}/{id}.
Mods are mapped to trade stat ids by their text: the trade site's stat texts are the game's stat descriptions with
numbers as '#', the same form as the planner's mod labels; local stats carry " (Local)". A target line with no
trade stat of that text is left out of the search and reported. If the short-link request fails, the link carries
the query itself (https://www.pathofexile.com/trade/search/{league}?q=...), which the trade site also accepts.
Network calls happen only when the user asks for a link (a click in the UI, --trade in the CLI).
Last verified against the live site: 2026-10-27 (current league "Allflame"; search ids are now long URL-safe
strings; the search endpoint is rate-limited per IP, e.g. 5 requests per 10 s - one request per click).
"""
from __future__ import annotations

import collections
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..messages import M
from .. import config
from .ninja import user_agent

SITE = "https://www.pathofexile.com"
STATS_TTL = 7 * 86400
LEAGUES_TTL = 3600
TIMEOUT = 20
GET_RETRY_AFTER = 60          # after a failed data download, use the cached copy (or fail fast) for this long
_get_failed: dict = {}        # name -> (when, reason) of the last failed data download
TYPES = ("explicit", "implicit", "fractured", "crafted")


class TradeError(Exception):
    pass


def _cache(name: str) -> Path:
    return config.DATA_DIR / f"trade_{name}.json"


def _get(path: str, name: str, ttl: int):
    """GET a trade-site data file, cached on disk for `ttl` seconds; a stale cache is used when the site is down."""
    cache = _cache(name)
    try:
        entry = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entry = None
    if entry and time.time() - entry.get("fetched", 0) < ttl:
        return entry["body"]
    failed = _get_failed.get(name)
    if failed and time.monotonic() - failed[0] < GET_RETRY_AFTER:
        # the site was just unreachable: don't make every request wait for the timeout again
        if entry:
            return entry["body"]
        raise TradeError(M("m.tradeerr.unreachable", reason=failed[1]))
    req = urllib.request.Request(SITE + path, headers={"User-Agent": user_agent(), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        _get_failed[name] = (time.monotonic(), str(getattr(e, "reason", e)))
        if entry:
            return entry["body"]
        raise TradeError(M("m.tradeerr.unreachable", reason=str(getattr(e, "reason", e)))) from None
    _get_failed.pop(name, None)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fetched": time.time(), "body": body}), encoding="utf-8")
    return body


def leagues(realm: str = "pc") -> list[str]:
    body = _get("/api/trade/data/leagues", "leagues", LEAGUES_TTL)
    return [x["id"] for x in body.get("result", []) if x.get("realm", "pc") == realm and x.get("id")]


def current_league(price_league: str | None = None) -> dict:
    """The league for trade links: {"league", "source", "known": [...]}.
    source: "prices" (the league the live prices come from, if the trade site lists it), "trade" (the first league
    the trade site lists - its current challenge league), or "fallback" (Standard: nothing could be fetched)."""
    try:
        known = leagues()
    except TradeError:
        known = []
    wanted = config.os.environ.get("POECRAFT_LEAGUE")
    if wanted and wanted in known:                      # the operator pinned a league
        return {"league": wanted, "source": "config", "known": known}
    if price_league and price_league in known:
        return {"league": price_league, "source": "prices", "known": known}
    if known:
        return {"league": known[0], "source": "trade", "known": known}
    return {"league": "Standard", "source": "fallback", "known": ["Standard"]}


def stat_index() -> dict[str, dict[str, str]]:
    """{"explicit": {text: trade id}, "implicit": {...}, "fractured": {...}, "crafted": {...},
    "_alts": {kind: {text: [every id with that text]}}} - a few texts belong to more than one stat (e.g. two
    "#% of Fire Damage Leeched as Life" implicits); searches then accept any of them."""
    body = _get("/api/trade/data/stats", "stats", STATS_TTL)
    out: dict = {t: {} for t in TYPES}
    alts: dict = {t: {} for t in TYPES}
    for group in body.get("result", []):
        if group.get("id") in out:
            for e in group.get("entries", []):
                out[group["id"]].setdefault(e["text"], e["id"])
                alts[group["id"]].setdefault(e["text"], []).append(e["id"])
    out["_alts"] = {k: {t: ids for t, ids in v.items() if len(ids) > 1} for k, v in alts.items()}
    return out


_UNIT_PLURAL = re.compile(r"\b(metre|second|minute|time|charge|target|projectile|enemy)s\b", re.I)


def _match(index: dict, kind: str, line: str, local: bool) -> tuple[str | None, str | None]:
    """(trade text, trade id) for one generic mod line. Local stats prefer ' (Local)'; unit words are compared
    singular/plural-blind (the trade site writes 'within # metre' where the mod says 'metres')."""
    table = index.get(kind, {})
    cands = (f"{line} (Local)", line) if local else (line, f"{line} (Local)")
    for c in cands:
        if c in table:
            return c, table[c]
    norm = {}
    for text in table:
        norm.setdefault(_UNIT_PLURAL.sub(lambda m: m.group(1), text).lower(), text)
    for c in cands:
        text = norm.get(_UNIT_PLURAL.sub(lambda m: m.group(1), c).lower())
        if text:
            return text, table[text]
    return None, None


def trade_stat(index: dict, kind: str, line: str, local: bool) -> str | None:
    """Trade stat id for one generic mod line ('+# to maximum Life') of `kind`."""
    return _match(index, kind, line, local)[1]


def trade_stat_ids(index: dict, kind: str, line: str, local: bool) -> list[str]:
    """Every trade stat id that a line may be (one, or several when the trade site has duplicate texts)."""
    text, tid = _match(index, kind, line, local)
    if tid is None:
        return []
    return list(index.get("_alts", {}).get(kind, {}).get(text, [tid]))


def stats_groups(filters: list[dict]) -> list[dict]:
    """The "stats" part of a query: one "and" group, plus an "at least one of" (count) group for every filter whose
    text has several trade stats (its "alts")."""
    plain = [{k: v for k, v in f.items() if k not in ("alts", "line")} for f in filters if len(f.get("alts", [])) < 2]
    groups = [{"type": "and", "filters": plain}]
    for f in filters:
        if len(f.get("alts", [])) >= 2:
            groups.append({"type": "count", "value": {"min": 1},
                           "filters": [dict({"id": a}, **({"value": f["value"]} if "value" in f else {})) for a in f["alts"]]})
    return groups


def build_query(type_name: str, filters: list[dict], rarity: str | None = "nonunique") -> dict:
    query = {"status": {"option": "online"}, "type": type_name, "stats": stats_groups(filters)}
    if rarity:
        query["filters"] = {"type_filters": {"filters": {"rarity": {"option": rarity}}}}
    return {"query": query, "sort": {"price": "asc"}}


def long_url(league: str, payload: dict) -> str:
    return (f"{SITE}/trade/search/{urllib.parse.quote(league)}?q="
            + urllib.parse.quote(json.dumps(payload, separators=(",", ":"))))


# Shared by every user of a public server: identical searches within TRADE_CACHE_TTL reuse the link; at most
# TRADE_MAX_PER_10S searches go to the trade site per 10 s; after a 429 nothing is sent until its Retry-After passed.
# When a search cannot be sent, callers give the long link (?q=...), which needs no request to the trade site.
_search_cache: dict = {}   # (league, query) -> (expires, link); not the disk-cache path helper _cache()
_sent: collections.deque = collections.deque()
_blocked_until = 0.0
_lock = threading.Lock()


def _reset_limits() -> None:            # tests
    _zeros.clear()
    _zeros.clear()
    _get_failed.clear()
    global _blocked_until
    with _lock:
        _search_cache.clear()
        _sent.clear()
        _blocked_until = 0.0
        for d in (globals().get("_listing_cache"), globals().get("_hits", {}).get("fetch")):
            if d is not None:
                d.clear()
        for k in globals().get("_paused", {}):
            _paused[k] = 0.0


SHORT_URL_WAIT = 8.0
CACHE_MAX = 2000              # entries per cache; expired ones go first


def _bounded(cache: dict, now: float, limit: int = CACHE_MAX) -> None:
    """Keep a {key: (expires, value)} cache bounded: drop expired entries, then the oldest (call with _lock held)."""
    if len(cache) < limit:
        return
    for k in [k for k, (exp, _) in cache.items() if exp <= now]:
        del cache[k]
    while len(cache) >= limit:
        cache.pop(next(iter(cache)))


def short_url(league: str, payload: dict) -> str:
    """The stable short link for a search: cached, rate-capped, then POSTed. Raises TradeError (callers fall back
    to long_url)."""
    global _blocked_until
    key = (league, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    now = time.monotonic()
    with _lock:
        hit = _search_cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        _bounded(_search_cache, now)
    # the same limiter as the evaluator's searches (one budget per IP): wait a few seconds for a slot, else the
    # caller falls back to the long link
    _slot("search", SHORT_URL_WAIT)
    try:
        url = _post_search(league, payload)
    except TradeError as e:
        if getattr(e, "retry_after", None):
            with _lock:
                _blocked_until = max(_blocked_until, time.monotonic() + e.retry_after)
        raise
    with _lock:
        _search_cache[key] = (time.monotonic() + config.TRADE_CACHE_TTL, url)
    return url


def _post_search(league: str, payload: dict) -> str:
    req = urllib.request.Request(f"{SITE}/api/trade/search/{urllib.parse.quote(league)}",
                                 json.dumps(payload).encode("utf-8"),
                                 {"User-Agent": user_agent(), "Content-Type": "application/json",
                                  "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = TradeError(f"trade site answered HTTP {e.code}")
        if e.code == 429:
            try:
                err.retry_after = float(e.headers.get("Retry-After") or 60)
            except ValueError:
                err.retry_after = 60.0
        raise err from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise TradeError(M("m.tradeerr.unreachable", reason=str(getattr(e, "reason", e)))) from None
    qid = body.get("id")
    # ids used to be short codes ("OQMzdqQUE"); since 2026 they are longer URL-safe strings ("H4sIAAAA...-_...")
    if not qid or not re.fullmatch(r"[A-Za-z0-9_-]+", str(qid)):
        raise TradeError(M("m.tradeerr.no_id"))
    return f"{SITE}/trade/search/{urllib.parse.quote(league)}/{qid}"


# ---------------------------------------------------------------- listings (Item Evaluator)
# GGG's limits per IP (2026-10): search 5 / 10 s, 15 / 60 s, 30 / 300 s; fetch 12 / 4 s, 16 / 12 s, 50 / 300 s, with
# lockouts of up to 30 min when exceeded. Every user of a public server shares its IP, so the caps below stay under
# them; after each answer the state GGG reports (X-Rate-Limit-Ip-State) is read and a rule close to its maximum
# pauses that kind of request for the rule's period.
# below each of GGG's search rules (5 / 10 s, 15 / 60 s, 30 / 300 s per IP - breaking one locks the IP out)
_CAPS = {"search": ((lambda: config.TRADE_MAX_PER_10S, 10), (lambda: config.TRADE_MAX_SEARCH_PER_MIN, 60),
                    (lambda: config.TRADE_MAX_SEARCH_PER_5MIN, 300)),
         "fetch": ((lambda: 10, 4), (lambda: 14, 12), (lambda: 45, 300))}
_hits: dict = {"search": _sent, "fetch": collections.deque()}       # "search" shares the "Find on Trade" counter
_paused: dict = {"search": 0.0, "fetch": 0.0}
_listing_cache: dict = {}


def _slot(kind: str, wait: float) -> None:
    """Take a request slot for `kind`, waiting up to `wait` seconds for one; TradeError if none comes free."""
    deadline = time.monotonic() + wait
    while True:
        with _lock:
            now = time.monotonic()
            hits = _hits[kind]
            while hits and now - hits[0] > 300:
                hits.popleft()
            free_at = max(_paused[kind], _blocked_until if kind == "search" else 0.0)
            for cap, period in _CAPS[kind]:
                recent = [t for t in hits if now - t < period]
                if len(recent) >= cap():
                    free_at = max(free_at, recent[0] + period)
            if free_at <= now:
                hits.append(now)
                return
        if free_at > deadline:
            raise TradeError(M("m.tradeerr.slot", s=round(free_at - now)))
        time.sleep(min(free_at - now, 1.0) + 0.05)


def _note_state(kind: str, headers) -> None:
    """Pause `kind` when GGG reports a rule within one request of its maximum."""
    rules, state = headers.get("X-Rate-Limit-Ip") or "", headers.get("X-Rate-Limit-Ip-State") or ""
    try:
        pairs = zip((r.split(":") for r in rules.split(",") if r), (s.split(":") for s in state.split(",") if s))
        for (mx, period, _), (cur, _, locked) in pairs:
            pause = float(locked) if float(locked) > 0 else (float(period) if int(cur) >= int(mx) - 1 else 0)
            if pause:
                with _lock:
                    _paused[kind] = max(_paused[kind], time.monotonic() + pause)
    except ValueError:
        pass


def _ggg(kind: str, req: urllib.request.Request, wait: float):
    global _blocked_until
    _slot(kind, wait)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            _note_state(kind, r.headers)
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            try:
                retry = float(e.headers.get("Retry-After") or 60)
            except ValueError:
                retry = 60.0
            with _lock:
                _paused[kind] = max(_paused[kind], time.monotonic() + retry)
                if kind == "search":
                    _blocked_until = max(_blocked_until, time.monotonic() + retry)
        raise TradeError(M("m.tradeerr.http", code=e.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise TradeError(M("m.tradeerr.unreachable", reason=str(getattr(e, "reason", e)))) from None


_zeros: list = []          # (expires, context, constraints) of searches that found nothing


def _constraints(league: str, payload: dict):
    """(context, {constraint: min}) of a search. Same context + a superset of a search's constraints (each minimum at
    least as high) can only find fewer items - so if that search found nothing, this one finds nothing either."""
    q = json.loads(json.dumps(payload.get("query", {})))
    cons = {}
    for g in q.pop("stats", []) or []:
        if g.get("type") == "and":
            for f in g.get("filters", []):
                v = f.get("value") or {}
                if set(v) <= {"min"}:
                    cons[("stat", f["id"])] = v.get("min")
                else:
                    cons[("stat", f["id"], json.dumps(v, sort_keys=True))] = None
        else:                                    # "count" groups etc.: one opaque constraint each
            cons[("group", json.dumps(g, sort_keys=True))] = None
    misc = ((q.get("filters") or {}).get("misc_filters") or {}).get("filters") or {}
    for k in list(misc):
        v = misc.pop(k)
        if isinstance(v, dict) and set(v) == {"min"}:
            cons[("misc", k)] = v["min"]
        else:
            cons[("misc", k, json.dumps(v, sort_keys=True))] = None
    filters = q.get("filters") or {}
    if "misc_filters" in filters and not (filters["misc_filters"].get("filters")):
        del filters["misc_filters"]              # moved into the constraints: no empty shell in the context
    if "filters" in q and not filters:
        del q["filters"]
    context = json.dumps([league, q, payload.get("sort")], sort_keys=True)
    return context, cons


def _known_zero(context: str, cons: dict) -> bool:
    now = time.monotonic()
    _zeros[:] = [z for z in _zeros if z[0] > now]
    for _, ctx, zc in _zeros:
        if ctx == context and all(k in cons and (zc[k] is None or (cons[k] is not None and cons[k] >= zc[k]))
                                  for k in zc):
            return True
    return False


def search_ids(league: str, payload: dict, wait: float = 20.0) -> dict:
    """POST a search: {"id", "total", "result": [listing ids, cheapest first]}; identical searches are cached, and a
    search stricter than one that found nothing is answered without asking ({"id": None, "total": 0, "inferred"})."""
    key = ("search", league, json.dumps(payload, sort_keys=True, separators=(",", ":")))
    context, cons = _constraints(league, payload)
    with _lock:
        hit = _listing_cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        if _known_zero(context, cons):
            return {"id": None, "total": 0, "result": [], "inferred": True}
    req = urllib.request.Request(f"{SITE}/api/trade/search/{urllib.parse.quote(league)}",
                                 json.dumps(payload).encode("utf-8"),
                                 {"User-Agent": user_agent(), "Content-Type": "application/json",
                                  "Accept": "application/json"})
    body = _ggg("search", req, wait)
    out = {"id": body.get("id"), "total": int(body.get("total") or 0), "result": list(body.get("result") or [])}
    with _lock:
        _bounded(_listing_cache, time.monotonic())
        _listing_cache[key] = (time.monotonic() + config.TRADE_CACHE_TTL, out)
        if not out["total"]:
            _zeros.append((time.monotonic() + config.TRADE_CACHE_TTL, context, cons))
            del _zeros[:-CACHE_MAX]
    return out


def fetch_listings(query_id: str, ids: list[str], wait: float = 20.0) -> list[dict]:
    """GET listing details (price, seller online status, item) for up to 10 ids per request, cached."""
    out = []
    for i in range(0, len(ids), 10):
        chunk = ids[i:i + 10]
        key = ("fetch", tuple(chunk))
        with _lock:
            hit = _listing_cache.get(key)
        if hit and hit[0] > time.monotonic():
            out += hit[1]
            continue
        req = urllib.request.Request(f"{SITE}/api/trade/fetch/{','.join(chunk)}?query={urllib.parse.quote(query_id)}",
                                     headers={"User-Agent": user_agent(), "Accept": "application/json"})
        rows = [r for r in (_ggg("fetch", req, wait).get("result") or []) if r]
        with _lock:
            _bounded(_listing_cache, time.monotonic())
            _listing_cache[key] = (time.monotonic() + config.TRADE_CACHE_TTL, rows)
        out += rows
    return out


def search_url(league: str, query_id: str) -> str:
    return f"{SITE}/trade/search/{urllib.parse.quote(league)}/{query_id}"
