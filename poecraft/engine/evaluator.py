"""Item Evaluator: price-check a pasted item on the official trade site, in the current league, online sellers first.

Rares: the first search uses every explicit / fractured / crafted mod, Eldritch implicits, influences and item level.
When it finds nothing, properties are dropped cheapest-to-recreate first. "Cheapest to recreate" is measured per item,
with the planner's own cost model and price data:
  * a modifier: the expected cost of crafting just that modifier at the tier this item rolled, on this base and item
    level (Planner, fast calculation) - a T1 Life roll costs far more than a T6 resistance or a bench craft;
  * an influence: free to drop when the item's influenced modifiers already imply it (they stay as filters), else the
    price of that influence's Exalted Orb (getting the influence at all);
  * item level: rarely what a finished rare is priced on, dropped first.
The core is never dropped: the most expensive half of the modifiers (at most CORE_MAX), each costing at least
CORE_SHARE of the most expensive one (which is always core).
The search budget per evaluation is small (GGG allows 30 searches per 5 minutes per IP, shared by every user), so
when more properties must go than searches remain, several are dropped together - still cheapest first.
Every search looks for online sellers; if none are found with only the core left, one search includes offline ones.
Results: up to MAX_LISTINGS listings, online first, then cheapest (prices converted to chaos with the live prices).
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from functools import lru_cache

from .. import config, i18n
from ..data import trade
from ..data.eldritch import get_eldritch
from ..data.repository import GameData, get_data, get_rules
from ..messages import CUR, J, M, N
from . import eldritch as eld
from .context import ItemContext
from .itemtext import FIELD, _norm, _sections, parse_item_text
from .tradelink import _price_league, add_filter, target_lines

MAX_LISTINGS = 15
CORE_SHARE = 0.35
CORE_MAX = 3
VALUE_SHARE = 0.9          # a filter's minimum is 90% of the item's own value (the usual price-check margin)
TIMELESS_BASE = "Metadata/Items/Jewels/JewelTimeless"
INFLUENCE_FILTERS = {"shaper": "shaper_item", "elder": "elder_item", "crusader": "crusader_item",
                     "redeemer": "redeemer_item", "hunter": "hunter_item", "warlord": "warlord_item"}
# trade price currency codes -> currency names (for the chaos value; unknown ones are shown as listed)
CURRENCY_CODES = {"chaos": "Chaos Orb", "divine": "Divine Orb", "exalted": "Exalted Orb", "alch": "Orb of Alchemy",
                  "alt": "Orb of Alteration", "vaal": "Vaal Orb", "fusing": "Orb of Fusing", "chrome": "Chromatic Orb",
                  "jewellers": "Jeweller's Orb", "regal": "Regal Orb", "gcp": "Gemcutter's Prism", "chance": "Orb of Chance",
                  "scour": "Orb of Scouring", "annul": "Orb of Annulment", "mirror": "Mirror of Kalandra",
                  "blessed": "Blessed Orb", "regret": "Orb of Regret", "transmute": "Orb of Transmutation",
                  "aug": "Orb of Augmentation"}
RARITY_WORDS = {"currency": ("currency", "валюта", "währung"), "divination": ("divination card", "гадальная карта",
                                                                              "weissagungskarte"),
                "gem": ("gem", "камень", "gemme")}
_CACHE: dict = {}
CACHE_MAX = 500               # cached evaluations (5 min each)
_LOCK = threading.Lock()


# ------------------------------------------------------------------ names of every item (for non-equipment)
@lru_cache(maxsize=1)
def _all_names() -> dict:
    """Normalized item name (English, Russian, German) -> (English name, item class, id), from base_items."""
    try:
        items = json.loads((config.RAW_DIR / "base_items.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in items.items():
        # unique-only bases too (Prismatic Jewel, Timeless Jewel ...): uniques are searched by name on them
        if v.get("release_state") not in ("released", "unique_only") or not v.get("name"):
            continue
        for lang in ("en", "ru", "de"):
            name = v["name"] if lang == "en" else i18n.game_text("base", k, v["name"], lang)[0]
            out.setdefault(_norm(name), (v["name"], v.get("item_class"), k))
    return out


def _header(text: str) -> dict:
    sections = _sections(text)
    flat = [l for s in sections for l in s]
    head = [l for l in (sections[0] if sections else []) if not (FIELD["item_class"].match(l) or FIELD["rarity"].match(l))]
    rarity = next((m.group(1).strip().lower() for l in flat if (m := FIELD["rarity"].match(l))), "")
    kind = next((k for k, words in RARITY_WORDS.items() if any(rarity.startswith(w) for w in words)), None)
    return {"flat": flat, "head": head[:3], "rarity": rarity, "kind": kind}


def _price_chaos(prices, price: dict | None):
    if not price or price.get("amount") is None:
        return None
    name = CURRENCY_CODES.get(price.get("currency"))
    if name is None:
        return None
    each = 1.0 if name == "Chaos Orb" else prices.get(name)
    return round(float(price["amount"]) * each, 2) if each else None


def _listing(prices, row: dict) -> dict:
    it, ls = row.get("item") or {}, row.get("listing") or {}
    mods = []
    for group in ("implicitMods", "explicitMods", "craftedMods", "fracturedMods"):
        for m in it.get(group) or []:
            mods.append(m.get("description") if isinstance(m, dict) else str(m))
    price = ls.get("price") or {}
    return {"price": {"amount": price.get("amount"), "currency": price.get("currency"), "type": price.get("type")},
            "chaos": _price_chaos(prices, price), "online": bool((ls.get("account") or {}).get("online")),
            "name": it.get("name") or "", "type": it.get("typeLine") or "", "ilvl": it.get("ilvl"), "mods": mods,
            "indexed": ls.get("indexed")}


def _sort(listings: list[dict]) -> list[dict]:
    """Online sellers first, then cheapest (unknown currencies after known prices)."""
    return sorted(listings, key=lambda x: (not x["online"], x["chaos"] is None, x["chaos"] or 0))


# ------------------------------------------------------------------ importance (rares)
_COST_CACHE: dict = {}


def _mod_cost(base_id: str, ilvl: int, influences, t: dict, kind: str) -> tuple[float | None, str]:
    """How hard this modifier is to get, in chaos, from the planner's fast calculation for just this modifier at the
    item's tier: (cost, basis). Rolled modifiers: the Chaos Orb spend to roll it (how rare the roll is - an essence
    would guarantee one modifier, so it says nothing about finding the combination); crafted: the bench cost (a
    buyer can redo it); Eldritch implicits: the Ember / Ichor cost of that tier."""
    from .planner import Infeasible, Planner
    from .prices import Prices
    spec = {"signature": t["key"], "min": t["min"]}
    if t.get("eldritch"):
        spec["eldritch"] = t["eldritch"]
    if t.get("fractured"):
        spec["fractured"] = True
    key = (base_id, ilvl, tuple(influences), kind, json.dumps(spec, sort_keys=True),
           json.dumps(Prices().live_status, sort_keys=True))
    if key in _COST_CACHE:
        return _COST_CACHE[key]
    try:
        pl = Planner(prices=Prices(), sims=0)
        pl.rec_time_limit = 1.0                      # the recovery variants do not change these single-mod costs
        r = pl.plan(base_id, ilvl, list(influences), [spec], lang=None)
        rows = [c for c in r["comparison"] if c.get("cost") is not None]
        if kind == "crafted" or t.get("eldritch"):
            pick, basis = rows, ("bench" if kind == "crafted" else "eldritch")
        else:
            pick, basis = [c for c in rows if str(c.get("family", "")).startswith("chaos")], "roll"
            if not pick:
                pick, basis = rows, "cost"
        out = (min(c["cost"] for c in pick), basis) if pick else (None, "unknown")
    except (Infeasible, ValueError, KeyError):
        out = (None, "unknown")
    if len(_COST_CACHE) >= 20000:
        _COST_CACHE.clear()                       # single-mod planner costs: cheap to recompute, never grow forever
    _COST_CACHE[key] = out
    return out


def _rank(data: GameData, parsed: dict, kinds: dict) -> list[dict]:
    """Every searchable property with its importance (chaos to recreate it) and whether it is core."""
    from .prices import Prices
    prices = Prices()
    base = data.bases[parsed["base_id"]]
    ctx = ItemContext(data, get_rules(), base, parsed["ilvl"], tuple(parsed["influences"]))
    mods = []
    for t in parsed["targets"]:
        kind = kinds.get(t["key"], "explicit")
        cost, basis = _mod_cost(base.id, parsed["ilvl"], parsed["influences"], t, kind)
        mods.append({"type": "mod", "target": t, "kind": kind, "cost": cost,
                     "why": M(f"m.eval.why.{basis}", c=round(cost, 1)) if cost is not None else M("m.eval.why.unknown")})
    for x in parsed.get("extra_implicits", []):    # Eldritch implicits the data does not list for this base
        mods.append({"type": "raw", "line": x["text"], "english": x["english"], "value": x["value"], "cost": None,
                     "why": M("m.eval.why.eld_unlisted")})
    known = sorted(m["cost"] for m in mods if m["cost"] is not None)
    median = known[len(known) // 2] if known else 1.0
    for m in mods:                      # unknown cost: ranked as a middling modifier, not dropped first or last
        m["score"] = m["cost"] if m["cost"] is not None else median
    influenced_ids = {h["id"] for h in parsed["have"] if h["id"] in ctx.influenced}
    els = list(mods)
    for inf in parsed["influences"]:
        implied = bool(influenced_ids)
        exalt = get_rules()["influence_exalts"].get(inf)
        price = prices.get(exalt) if exalt else 0.0
        els.append({"type": "influence", "influence": inf, "score": 0.0 if implied else price,
                    "why": M("m.eval.why.implied") if implied else M("m.eval.why.influence", c=round(price, 1),
                                                                       exalt=CUR(exalt) if exalt else "")})
    els.append({"type": "ilvl", "ilvl": parsed["ilvl"], "score": -1.0, "why": M("m.eval.why.ilvl")})
    # the absolute core: the most expensive half of the modifiers (at most CORE_MAX), each costing at least
    # CORE_SHARE of the most expensive one; the most expensive modifier is always core
    ranked = sorted(mods, key=lambda m: -m["score"])
    top = ranked[0]["score"] if ranked else 0.0
    n_core = max(1, min(CORE_MAX, math.ceil(len(ranked) / 2)))
    core = {id(m) for i, m in enumerate(ranked) if i == 0 or (i < n_core and m["score"] >= CORE_SHARE * top)}
    for e in els:
        e["core"] = id(e) in core
    return sorted(els, key=lambda e: -e["score"])


def _label(e: dict, lines_by_key: dict):
    if e["type"] == "mod":
        return lines_by_key.get(e["target"]["key"], e["target"]["label"])     # as pasted, in the user's language
    if e["type"] == "raw":
        return e["line"]
    if e["type"] == "influence":
        return M("m.eval.el.influence", inf=N(e["influence"].title()))
    return M("m.eval.el.ilvl", n=e["ilvl"])


# ------------------------------------------------------------------ queries
def _query(base_name: str, kept: list[dict], fams, eld_fams, index, status: str, unmapped: list) -> dict:
    filters, misc = [], {}
    for e in kept:
        if e["type"] == "mod":
            t = dict(e["target"])
            v = t.get("value")
            t["min"] = None if v is None else (math.floor(v * VALUE_SHARE) if v >= 10 else round(v * VALUE_SHARE, 2))
            kind = "crafted" if e["kind"] == "crafted" else None
            lines, miss = target_lines(t, fams, eld_fams, index, kind=kind)
            unmapped += [x for x in miss if x not in unmapped]
            for f in lines:
                add_filter(filters, {k: v for k, v in f.items() if k != "line"})
        elif e["type"] == "raw":                 # an Eldritch implicit by its text: the trade site's implicit stat
            ids = trade.trade_stat_ids(index, "implicit", e["english"], False) if e.get("english") else []
            if not ids:
                if e["line"] not in unmapped:
                    unmapped.append(e["line"])
                continue
            v = e.get("value")
            f = {"id": ids[0]}
            if len(ids) > 1:
                f["alts"] = ids
            if v is not None:
                f["value"] = {"min": math.floor(v * VALUE_SHARE) if v >= 10 else round(v * VALUE_SHARE, 2)}
            add_filter(filters, f)
        elif e["type"] == "influence":
            misc[INFLUENCE_FILTERS[e["influence"]]] = {"option": "true"}
        elif e["type"] == "ilvl":
            misc["ilvl"] = {"min": e["ilvl"]}
    q = {"status": {"option": status}, "type": base_name, "stats": trade.stats_groups(filters),
         "filters": {"type_filters": {"filters": {"rarity": {"option": "nonunique"}}}}}
    if misc:
        q["filters"]["misc_filters"] = {"filters": misc}
    return {"query": q, "sort": {"price": "asc"}}


def _plan_drops(droppable: list[dict], passes_left: int) -> list[list[dict]]:
    """Split the droppable properties (cheapest first) into at most `passes_left` cumulative steps."""
    if not droppable or passes_left <= 0:
        return []
    n = len(droppable)
    size = math.ceil(n / passes_left)
    return [droppable[:min(n, (i + 1) * size)] for i in range(math.ceil(n / size))]


# ------------------------------------------------------------------ entry
def evaluate(text: str, league: str | None = None, data=None, socket=None, equivalents=None):
    from .prices import PRICE_LEAGUE, Prices
    lg = trade.current_league(_price_league())
    selected = league or lg["league"]
    token = PRICE_LEAGUE.set(selected)
    try:
        result = _evaluate_with_context(text, league, data, socket, equivalents)
        status = Prices().live_status
        result = dict(result, economy={k: v for k, v in status.items() if k != "prices"})
        result["warnings"] = list(result.get("warnings", []))
        if status.get("message_key"):
            result["warnings"].append(M(status["message_key"], **status.get("message_params", {})))
        return _notes(result)
    finally:
        PRICE_LEAGUE.reset(token)


def _evaluate_with_context(text: str, league: str | None = None, data: GameData | None = None, socket: str | None = None,
             equivalents: list | None = None) -> dict:
    """See the module docstring. Returns {"kind", "league", "league_source", "item", "ranking", "passes",
    "listings", "summary" (cheapest + online count, or None), "search_url", "message"?, "ninja_price"?, "warnings"}
    (messages as Text objects)."""
    data = data or get_data()
    lg = trade.current_league(_price_league())
    if league:
        lg = {"league": league, "source": "user", "known": lg["known"]}
    from .prices import Prices
    key = (lg["league"], text.strip(), json.dumps(Prices().live_status, sort_keys=True))
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            out = dict(hit[1], cached=True)
            return _timeless_extras(out, socket, equivalents, lg)
    out = _evaluate(text, lg, data)
    out.pop("_deadline", None)
    out.pop("_status", None)
    out["summary"] = _summary(out)
    with _LOCK:
        now = time.monotonic()
        if len(_CACHE) >= CACHE_MAX:              # bounded: expired entries first, then the oldest
            for k in [k for k, (exp, _) in _CACHE.items() if exp <= now]:
                del _CACHE[k]
            while len(_CACHE) >= CACHE_MAX:
                _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (now + config.TRADE_CACHE_TTL, out)
    return _timeless_extras(out, socket, equivalents, lg)


def _summary(out: dict) -> dict | None:
    """The price at a glance: the cheapest listing (an online seller's when there is one) and how many listings
    online sellers have - the trade site's total for the last online search, not just the ones fetched."""
    listings = out.get("listings") or []
    if not listings:
        return None
    online = [l for l in listings if l["online"]]
    pool = [l for l in (online or listings) if l.get("chaos") is not None] or (online or listings)
    cheapest = min(pool, key=lambda l: l["chaos"] if l.get("chaos") is not None else float("inf"))
    last = out["passes"][-1] if out.get("passes") else {}
    n_online = last.get("total") if last.get("status") == "online" and last.get("total") else len(online)
    return {"price": cheapest["price"], "chaos": cheapest.get("chaos"), "online": n_online, "offline_only": not online,
            "shown": len(listings)}


def _base_result(lg: dict) -> dict:
    out = {"league": lg["league"], "league_source": lg["source"], "ranking": [], "passes": [], "listings": [],
           "search_url": None, "warnings": []}
    if lg["source"] == "fallback":
        out["warnings"].append(M("m.trade.league_fallback"))
    return out


def _evaluate(text: str, lg: dict, data: GameData) -> dict:
    out = _base_result(lg)
    h = _header(text)
    # a corrupted / mirrored item is compared with items in the same state (every search of this evaluation)
    out["_status"] = _status_filters(text)
    if out["_status"]:
        out["warnings"].append(M("m.eval.status", what=J([M(f"eval.status.{k}") for k in out["_status"]], ", ")))
    names = _all_names()
    hits = [names[_norm(l)] for l in h["head"] if _norm(l) in names]
    # Timeless Jewels: the value is in the seed - searched by jewel + conqueror + seed, effects from the calculator
    from . import timeless as tj
    info = tj.parse(text)
    if info or any(x[2] == TIMELESS_BASE for x in hits) or any(
            "timeless jewel" in _norm(l) or "вневременной самоцвет" in _norm(l) for l in h["flat"][:5]):
        return _timeless(out, info, lg)
    rarity = h["rarity"]
    if any(w in rarity for w in ("unique", "уникальн", "einzigartig")):
        # a unique is searched by its name - never checked against the crafting data (its base may not be craftable)
        base = next((x for x in hits if x[2] != TIMELESS_BASE), None) or _contained_base(h["head"], names)
        return _unique(out, h, base, lg)
    parsed = parse_item_text(text, data) if not h["kind"] else {"warnings": []}
    if parsed.get("base_id") and parsed.get("rarity") in ("rare", "magic") and (parsed.get("targets")
                                                                               or parsed.get("extra_implicits")):
        return _rare(out, parsed, lg, data)
    if parsed.get("base_id"):
        return _by_type(out, data.bases[parsed["base_id"]].name, lg, "base", parsed)
    if hits:
        if rarity_is(rarity, ("rare", "редк", "selten", "magic", "волшебн", "magisch")):
            # a rare / magic item of a class the crafting data does not hold (jewels, maps ...): only its type is searched
            out["warnings"].append(M("m.eval.type_only", base=hits[0][0], what=_unfiltered(hits[0])))
        return _by_type(out, hits[0][0], lg, h["kind"] or "other", None)
    base = _contained_base(h["head"], names)
    if base:
        # a base the crafting data does not hold (Tinctures, ...) inside a magic item's name: search by base type
        out["warnings"].append(M("m.eval.type_only", base=base[0], what=_unfiltered(base)))
        return _by_type(out, base[0], lg, "other", None)
    out.update(kind="unknown", message=M("m.eval.unknown_item", lines=" / ".join(h["head"])))
    return out


def _timeless(out: dict, info: dict | None, lg: dict) -> dict:
    """A Timeless Jewel: what it is (jewel, conqueror, seed), the sockets for the calculator, and the trade listings
    of exactly this jewel + conqueror + seed. The effects need a socket: evaluate(..., socket=...) adds them."""
    from ..data import timeless as td
    from . import timeless as tj
    out.update(kind="timeless")
    if not info:
        out["message"] = M("m.eval.timeless_unread")
        return out
    out["item"] = {"name": info["jewel"], "type": "Timeless Jewel"}
    lo, hi = td.SEEDS[info["version"]]
    div = td.SEED_DIVISOR.get(info["version"], 1)
    t = {k: info[k] for k in ("version", "jewel", "seed", "conqueror")}
    t.update(seed_range=[lo * div, hi * div], valid=tj.valid_seed(info["version"], info["seed"]), data=td.available())
    if t["data"]:
        t["sockets"] = tj.sockets()
    else:
        out["warnings"].append(M("m.eval.timeless_no_data"))
    if not t["valid"]:
        out["warnings"].append(M("m.eval.timeless_seed", seed=info["seed"], lo=lo * div, hi=hi * div))
    out["timeless"] = t
    try:
        p = {"status": "online", "dropped": [], "total": None}
        out["passes"].append(p)
        _with_offline_fallback(out, lg, lambda status: tj.trade_payload(info, status), p)
    except trade.TradeError as e:
        out["warnings"].append(M("m.eval.trade_error", error=e.args[0] if e.args else str(e)))
    return out


# notes that are real failures (shown as such); everything else is information about what could not be matched
ERROR_NOTES = {"m.eval.trade_error", "m.trade.no_stats", "m.eval.timeless_calc_error", "m.trade.short_failed"}
# notes about one pasted line: the UI offers to copy it, for adding it on the trade site by hand
LINE_NOTES = {"m.imp.eld_unlisted", "m.trade.unmapped", "m.imp.unknown", "m.imp.not_on_base",
              "m.imp.not_on_base_defence", "m.imp.approx"}


def _notes(out: dict) -> dict:
    """The warnings as notes: {"text", "level": error / info, "copy": the mod line or None}."""
    notes = []
    for w in out.get("warnings", []):
        key = getattr(w, "key", None)
        params = getattr(w, "params", {}) or {}
        line = params.get("line") if key in LINE_NOTES else None
        notes.append({"text": w, "level": "error" if key in ERROR_NOTES else "info",
                      "copy": str(line) if line is not None else None})
    return dict(out, notes=notes)


def _timeless_extras(out: dict, socket, equivalents, lg: dict) -> dict:
    """A Timeless result plus, for a socket, the effects - and, when asked, the equivalent seeds and their prices."""
    if not socket:
        return _notes(out)
    out = _with_calculation(out, socket)
    if equivalents is not None and out.get("timeless", {}).get("calc"):
        out = _with_equivalents(out, socket, [str(n) for n in equivalents], lg)
    return _notes(out)


SCAN_LISTINGS = 100           # cheapest listings of this jewel + conqueror (any seed) checked for equivalent seeds


def _with_equivalents(out: dict, socket: str, node_ids: list[str], lg: dict) -> dict:
    """Seeds of the same jewel and conqueror that change the chosen notables the same way (or better), priced on the
    trade site in two searches: (1) the market scan - every listed jewel of this jewel + conqueror, cheapest first;
    the seeds of the cheapest SCAN_LISTINGS listings are checked against the equivalent seeds (the comparison is
    local and instant) - this finds the cheapest listed equivalents however many equivalent seeds there are;
    (2) one "at least one of these seeds" search for equivalents the scan did not reach. The seed of each listing is
    read from its text."""
    from ..data import timeless as td
    from . import timeless as tj
    from .prices import Prices
    t = out["timeless"]
    info = {"jewel": t["jewel"], "conqueror": t["conqueror"], "seed": t["seed"]}
    try:
        eq = tj.equivalents(t["version"], t["seed"], str(socket), node_ids or None, limit=10 ** 6)
    except (ValueError, td.TimelessDataMissing) as e:
        out["warnings"] = list(out["warnings"]) + [M("m.eval.timeless_calc_error", error=e.args[0] if e.args else str(e))]
        return out
    exact = [x for x in eq["seeds"] if x["matched"] == x["of"]]
    candidates = exact or eq["seeds"][:tj.MAX_EQUIVALENTS * 2]                      # exact, else the closest
    by_seed = {x["seed"]: dict(x, listing=None) for x in candidates}
    res = {"core": eq["core"], "checked": eq["checked"], "exact": eq["exact"], "closest": not exact,
           "found": len(candidates), "scanned": 0, "seeds": [], "search_url": None}
    prices = Prices()
    budget: dict = {}
    lo, hi = td.SEEDS[t["version"]]
    div = td.SEED_DIVISOR.get(t["version"], 1)
    try:
        if candidates:
            # (1) the market scan: all seeds of this jewel + conqueror, cheapest first
            scan = tj.trade_payload_range(info, lo * div, hi * div, "online")
            res["scanned"] = _collect(scan, SCAN_LISTINGS, lg, by_seed, prices, budget)
            # (2) the equivalents the scan did not reach, by seed
            rest = [x for x in candidates if by_seed[x["seed"]]["listing"] is None][:tj.MAX_EQUIVALENTS]
            if rest:
                url = _collect(tj.trade_payload_seeds(info, [x["seed"] for x in rest], "online"), MAX_LISTINGS, lg,
                               by_seed, prices, budget, want_url=True)
                res["search_url"] = url
                if not any(v["listing"] for v in by_seed.values()):     # nobody online: offline sellers too
                    res["search_url"] = _collect(tj.trade_payload_seeds(info, [x["seed"] for x in rest], "any"),
                                                 MAX_LISTINGS, lg, by_seed, prices, budget, want_url=True) or url
    except trade.TradeError as e:
        out["warnings"] = list(out["warnings"]) + [M("m.eval.trade_error", error=e.args[0] if e.args else str(e))]
    listed = [v for v in by_seed.values() if v["listing"]]
    unlisted = [v for v in by_seed.values() if not v["listing"]][:15]
    rows = listed + unlisted
    for r in rows:
        r["url"] = trade.long_url(lg["league"], tj.trade_payload(dict(info, seed=r["seed"]), "any"))
    rows.sort(key=lambda r: (r["listing"] is None, not (r["listing"] or {}).get("online", False),
                             (r["listing"] or {}).get("chaos") if (r["listing"] or {}).get("chaos") is not None else 1e18,
                             -r["matched"], r["seed"]))
    res.update(seeds=rows, listed=len(listed), priced=len(listed))
    return dict(out, timeless=dict(out["timeless"], equivalents=res))


def _collect(payload, n, lg, by_seed, prices, budget, want_url=False):
    """Run one search, fetch its cheapest `n` listings, keep the cheapest listing per wanted seed. Returns how many
    listings were looked at (or the search's link, with want_url)."""
    from . import timeless as tj
    found = trade.search_ids(lg["league"], payload, wait=_wait(budget))
    url = trade.search_url(lg["league"], found["id"]) if found.get("id") else None
    rows = trade.fetch_listings(found["id"], found["result"][:n], wait=_wait(budget)) if found["total"] else []
    key = lambda l: (not l["online"], l["chaos"] if l["chaos"] is not None else 1e18)
    for row in rows:
        lst = _listing(prices, row)
        cur = by_seed.get(tj.listing_seed(lst["mods"]))
        if cur is not None and (cur["listing"] is None or key(lst) < key(cur["listing"])):
            cur["listing"] = {"price": lst["price"], "chaos": lst["chaos"], "online": lst["online"]}
    return url if want_url else len(rows)


def _with_calculation(out: dict, socket: str) -> dict:
    """A copy of a Timeless result with the effects for one socket (cheap: a few table reads, not cached)."""
    from ..data import timeless as td
    from . import timeless as tj
    t = out.get("timeless")
    if not t or not t.get("data") or not t.get("valid"):
        return out
    out = dict(out, timeless=dict(t))
    try:
        calc = tj.calculate(t["version"], t["seed"], t["conqueror"], str(socket))
    except (ValueError, td.TimelessDataMissing) as e:
        out["warnings"] = list(out["warnings"]) + [M("m.eval.timeless_calc_error", error=e.args[0] if e.args else str(e))]
        return out
    for e in calc["nodes"]:                 # passive names and stats are the tree export's English texts
        e["name"] = N(e["name"])
        if e.get("new_name"):
            e["new_name"] = N(e["new_name"])
    out["timeless"]["calc"] = calc
    return out


def _wait(out: dict) -> float:
    """How long the next request may wait for a slot: what is left of this evaluation's budget (so relaxed searches
    queue behind GGG's limits instead of failing, but the page never hangs for minutes)."""
    deadline = out.setdefault("_deadline", time.monotonic() + config.EVAL_WAIT_BUDGET)
    return max(0.0, deadline - time.monotonic())


def _search_and_fetch(out: dict, lg: dict, payload: dict) -> int:
    from .prices import Prices
    res = trade.search_ids(lg["league"], _with_status(payload, out.get("_status")), wait=_wait(out))
    if res.get("inferred"):
        out["inferred"] = out.get("inferred", 0) + 1       # answered from an earlier search that found nothing
    elif res.get("id"):
        out["search_url"] = trade.search_url(lg["league"], res["id"])
    if res["total"]:
        rows = trade.fetch_listings(res["id"], res["result"][:MAX_LISTINGS], wait=_wait(out))
        prices = Prices()
        out["listings"] = _sort([_listing(prices, r) for r in rows])
    return res["total"]


def _with_offline_fallback(out: dict, lg: dict, make, note_pass: dict | None = None) -> None:
    total = _search_and_fetch(out, lg, make("online"))
    if note_pass is not None:
        note_pass["total"] = total
    if total == 0:
        total = _search_and_fetch(out, lg, make("any"))
        out["passes"].append({"status": "any", "dropped": [], "total": total})
        out["warnings"].append(M("m.eval.offline_fallback") if total else M("m.eval.none_found"))


def _by_type(out: dict, type_name: str, lg: dict, kind: str, parsed) -> dict:
    """Uniques by base, bases, currency, cards, fragments, gems: search by item name."""
    out.update(kind=kind, item={"type": type_name})
    if parsed:
        out["item"].update(ilvl=parsed.get("ilvl"), influences=parsed.get("influences", []))
    if kind == "currency":                   # poe.ninja's price next to the listings (live data only)
        from .prices import Prices
        p = Prices()
        if p.source(type_name) == "live":
            out["ninja_price"] = p.get(type_name)
    if kind == "gem":
        out["warnings"].append(M("m.eval.gem_note"))

    def make(status):
        q = {"status": {"option": status}, "type": type_name}
        if kind == "base" and parsed:
            misc = {INFLUENCE_FILTERS[i]: {"option": "true"} for i in parsed.get("influences", [])}
            misc["ilvl"] = {"min": parsed["ilvl"]}
            q["filters"] = {"misc_filters": {"filters": misc}}
        return {"query": q, "sort": {"price": "asc"}}
    try:
        p = {"status": "online", "dropped": [], "total": None}
        out["passes"].append(p)
        _with_offline_fallback(out, lg, make, p)
    except trade.TradeError as e:
        out["warnings"].append(M("m.eval.trade_error", error=e.args[0] if e.args else str(e)))
    return out


def _unfiltered(base: tuple):
    """What is not filtered, by item class: "Tincture mods", "Jewel mods" ... (else "this item's modifiers")."""
    cls = str(base[1] or "").lower()
    key = next((k for k in ("tincture", "jewel", "map", "flask") if k in cls), None)
    return M(f"eval.unfiltered.{key}" if key else "eval.unfiltered.other")


def rarity_is(rarity: str, words) -> bool:
    return any(rarity.startswith(w) for w in words)


def _status_filters(text: str) -> dict:
    """misc filters for a corrupted / mirrored item: compare it with items in the same state."""
    from .itemtext import _status
    return {st: {"option": "true"} for st in sorted(_status(_sections_flat(text)))}


def _sections_flat(text: str) -> list[str]:
    return [l for s in _sections(text) for l in s]


def _with_status(payload: dict, misc: dict) -> dict:
    if misc:
        f = payload["query"].setdefault("filters", {}).setdefault("misc_filters", {}).setdefault("filters", {})
        f.update(misc)
    return payload


def _contained_base(head: list[str], names: dict) -> tuple | None:
    """The longest known base name inside a header line ("Concentrated Prismatic Tincture of Empowering" ->
    Prismatic Tincture), for magic items whose base the crafting data does not hold."""
    best = None
    for line in head:
        low = _norm(line)
        for n, v in names.items():
            if len(n) > 3 and (best is None or len(n) > len(best[0])) and re.search(rf"(^|\s){re.escape(n)}($|\s)", low):
                best = (n, v)
    return best[1] if best else None


def _unique(out: dict, h: dict, base: tuple | None, lg: dict) -> dict:
    """A unique by its name (English names; a localized one by its base type, all uniques on it)."""
    base_name = base[0] if base else None
    base_names = {_norm(base_name)} | {_norm(i18n.game_text("base", base[2], base_name, c)[0]) for c in ("ru", "de")} \
        if base else set()
    name = next((l for l in h["head"] if _norm(l) not in base_names), None)
    ascii_name = bool(name) and all(ord(ch) < 128 for ch in name)
    ilvl = next((int(m.group(1)) for l in h["flat"] if (m := FIELD["ilvl"].match(l))), None)
    if not ascii_name and not base_name:
        out.update(kind="unknown", message=M("m.eval.unknown_item", lines=" / ".join(h["head"])))
        return out
    out.update(kind="unique", item={"name": name, "type": base_name, "ilvl": ilvl})
    if not ascii_name:
        out["warnings"].append(M("m.eval.unique_name", base=base_name))

    def make(status):
        q = {"status": {"option": status}, "filters": {"type_filters": {"filters": {"rarity": {"option": "unique"}}}}}
        if base_name:
            q["type"] = base_name
        if ascii_name:
            q["name"] = name
        return {"query": q, "sort": {"price": "asc"}}
    try:
        p = {"status": "online", "dropped": [], "total": None}
        out["passes"].append(p)
        _with_offline_fallback(out, lg, make, p)
    except trade.TradeError as e:
        out["warnings"].append(M("m.eval.trade_error", error=e.args[0] if e.args else str(e)))
    return out


def _rare(out: dict, parsed: dict, lg: dict, data: GameData) -> dict:
    base = data.bases[parsed["base_id"]]
    # (a "presence" line is the upgrade of an implicit already read: the implicit's own line describes it)
    read = [l for l in parsed["lines"] if l.get("matched") and l["kind"] != "presence"]
    kinds = {l["key"]: l["kind"] for l in read}
    lines_by_key = {l["key"]: l["text"] for l in read}
    out.update(kind="rare", item={"type": base.name, "ilvl": parsed["ilvl"], "influences": parsed["influences"],
                                  "rarity": parsed["rarity"]})
    # the importer's notes about planner "have" mode do not apply to a price check
    planner_only = {"m.imp.eldritch_have", "m.imp.crafted", "m.imp.fractured_have"}
    out["warnings"] += [w for w in parsed["warnings"] if getattr(w, "key", None) not in planner_only]
    ranking = _rank(data, parsed, kinds)
    for e in ranking:
        e["label"] = _label(e, lines_by_key)
    out["ranking"] = [{"label": e["label"], "why": e["why"], "core": e["core"], "type": e["type"],
                       "score": round(e["score"], 1) if e["score"] >= 0 else None} for e in ranking]
    try:
        index = trade.stat_index()
    except trade.TradeError as e:
        out["warnings"].append(M("m.trade.no_stats", error=e.args[0] if e.args else str(e)))
        return out
    ctx = ItemContext(data, get_rules(), base, parsed["ilvl"], tuple(parsed["influences"]))
    fams = ctx.families()
    eld_fams = eld.base_families(get_eldritch(), base.item_class, ctx.tags) if not ctx.influences else {}
    unmapped: list = []
    droppable = [e for e in reversed(ranking) if not e["core"]]        # cheapest to recreate first
    budget = max(1, config.EVAL_MAX_SEARCHES)
    steps = [[]] + _plan_drops(droppable, budget - 2)                  # one search stays for the offline fallback
    try:
        for dropped in steps:
            kept = [e for e in ranking if e not in dropped]
            payload = _query(base.name, kept, fams, eld_fams, index, "online", unmapped)
            total = _search_and_fetch(out, lg, payload)
            out["passes"].append({"status": "online", "dropped": [e["label"] for e in dropped], "total": total})
            if total:
                break
        else:
            kept = [e for e in ranking if e not in steps[-1]]
            total = _search_and_fetch(out, lg, _query(base.name, kept, fams, eld_fams, index, "any", unmapped))
            out["passes"].append({"status": "any", "dropped": [e["label"] for e in steps[-1]], "total": total})
            out["warnings"].append(M("m.eval.offline_fallback") if total else M("m.eval.no_results"))
    except trade.TradeError as e:
        out["warnings"].append(M("m.eval.trade_error", error=e.args[0] if e.args else str(e)))
    out["warnings"] += [M("m.trade.unmapped", line=u) for u in unmapped]
    return out
