"""'Find on Trade': the planner's targets as a search on the official trade site, in the current league.

Each target becomes one stat filter per line of its mod (hybrids have two lines); the threshold is the minimum of the
first line, as everywhere in the planner. Explicit targets search explicit stats, fractured targets fractured stats,
Eldritch implicits implicit stats. Lines the trade site has no stat for are left out and reported.
"""
from __future__ import annotations

from ..data import trade
from ..data.eldritch import get_eldritch
from ..data.repository import GameData, get_data, get_rules
from ..messages import M
from . import eldritch as eld
from .context import ItemContext


def _price_league() -> str | None:
    try:
        from ..data import ninja
        return ninja.load_status().get("league")
    except Exception:  # noqa: BLE001 - no price data is fine: the trade site's own list decides then
        return None


def target_lines(t: dict, fams: dict, eld_fams: dict, index: dict, kind: str | None = None) -> tuple[list, list]:
    """Trade stat filters for one planner target: ([{"id", "value"?, "line"}], [lines without a trade stat]).
    One filter per line of the mod (hybrids have two); the threshold goes on the first line. `kind` overrides the
    stat group (e.g. "crafted"); by default explicit, fractured for fractured targets, implicit for Eldritch."""
    sig = t.get("signature") or t.get("key")
    if t.get("eldritch"):
        fam = eld_fams.get(sig)
        label, stats, group = (fam.label, [fam.primary_stat], "implicit") if fam else (None, [], "implicit")
    else:
        fam = fams.get(sig)
        label = fam.label if fam else t.get("label")
        stats = sig.split("|", 1)[1].split(",") if "|" in sig else []
        group = "fractured" if t.get("fractured") else "explicit"
    group = kind or group
    if not label:
        return [], [t.get("label") or sig]
    local = any(s.startswith("local_") for s in stats)
    out, unmapped = [], []
    for i, line in enumerate(label.split(" / ")):
        ids = trade.trade_stat_ids(index, group, line, local)
        if not ids:
            unmapped.append(line)
            continue
        f = {"id": ids[0], "line": line}
        if len(ids) > 1:
            f["alts"] = ids                        # the trade site has this text more than once: any of them
        if i == 0 and t.get("min") not in (None, ""):
            f["value"] = {"min": t["min"]}
        out.append(f)
    return out, unmapped


def add_filter(filters: list, f: dict) -> None:
    """The trade site adds up one stat over all mods (e.g. a hybrid's Life and a Life prefix): one filter per stat,
    with the larger minimum."""
    same = next((x for x in filters if x["id"] == f["id"]), None)
    if same is None:
        filters.append(dict(f))
        return
    lo = f.get("value", {}).get("min")
    if lo is not None and lo > same.get("value", {}).get("min", float("-inf")):
        same["value"] = {"min": lo}


def trade_search(base_id: str, ilvl: int, influences, targets: list[dict], league: str | None = None,
                 shorten: bool = True, data: GameData | None = None) -> dict:
    """targets: planner target specs ({"signature", "min", "fractured"?, "eldritch"?}). Returns
    {"url", "short", "league", "league_source", "leagues", "filters": [...], "warnings": [M ...], "payload"}."""
    data = data or get_data()
    base = data.bases[base_id]
    warnings: list = []
    lg = trade.current_league(_price_league())
    if league:
        lg = {"league": league, "source": "user", "known": lg["known"]}
    if lg["source"] == "fallback":
        warnings.append(M("m.trade.league_fallback"))
    try:
        index = trade.stat_index()
    except trade.TradeError as e:
        index = {}
        warnings.append(M("m.trade.no_stats", error=e.args[0] if e.args else str(e)))
    ctx = ItemContext(data, get_rules(), base, int(ilvl), tuple(influences or ()))
    fams = ctx.families()
    eld_fams = eld.base_families(get_eldritch(), base.item_class, ctx.tags) if not ctx.influences else {}
    filters, mapped = [], []
    for t in targets:
        lines, unmapped = target_lines(t, fams, eld_fams, index)
        warnings += [M("m.trade.unmapped", line=u) for u in unmapped]
        for f in lines:
            new = all(x["id"] != f["id"] for x in filters)
            add_filter(filters, f)
            if new:
                mapped.append({"line": f["line"], "id": f["id"], "min": f.get("value", {}).get("min")})
    filters = [{k: v for k, v in f.items() if k != "line"} for f in filters]       # build_query groups the alts
    payload = trade.build_query(base.name, filters)
    url, short = None, False
    if shorten:
        try:
            url, short = trade.short_url(lg["league"], payload), True
        except trade.TradeError as e:
            warnings.append(M("m.trade.short_failed", error=e.args[0] if e.args else str(e)))
    if url is None:
        url = trade.long_url(lg["league"], payload)
    return {"url": url, "short": short, "league": lg["league"], "league_source": lg["source"],
            "leagues": lg["known"], "filters": mapped, "warnings": warnings, "payload": payload}
