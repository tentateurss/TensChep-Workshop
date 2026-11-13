"""Timeless Jewels: read the jewel from pasted text, list the sockets, and say what the seed does to every passive in
the socket's radius - "Node: old stats -> new stats" - from Path of Building's tables (see data/timeless.py).

What changes, by passive type (Path of Building's rules, PassiveSpec.lua):
  * keystones: replaced by the conqueror's keystone;
  * notables: from the table - an addition ("+X% ..." on top of the notable) or a replacement passive; Glorious
    Vanity also stores the rolls, and its "Might / Legacy of the Vaal" notables carry 3-4 additions;
  * small passives: Glorious Vanity from the table; the others get a fixed change (+4 Strength for the Karui, +2 on
    an attribute passive; Dexterity for the Maraketh; Devotion for the Templars; Ward for the Kalguur; the Eternal
    Empire blanks them).
Masteries, sockets and class starts are not affected.
"""
from __future__ import annotations

import math
import re

from ..data import timeless as T
from ..messages import M

ATTRIBUTE_NODES = ("Strength", "Dexterity", "Intelligence")
# the seed lines in English, for reading a jewel before the Timeless data has been downloaded
ENGLISH_LINES = {
    1: r"bathed in the blood of (\d+) sacrificed in the name of (\w+)",
    2: r"commanded leadership over (\d+) warriors under (\w+)",
    3: r"denoted service of (\d+) dekhara in the akhara of (\w+)",
    4: r"carved to glorify (\d+) new faithful converted by high templar (\w+)",
    5: r"commissioned (\d+) coins to commemorate (\w+)",
    6: r"remembrancing (\d+) songworthy deeds by the line of (\w+)",
}
# conqueror -> (keystone index, revision): Path of Building's ModParser.lua conquerorList
CONQUERORS = {
    1: {"Xibaqua": (1, 0), "Zerphi": (2, 0), "Ahuana": (2, 1), "Doryani": (3, 0)},
    2: {"Kaom": (1, 0), "Rakiata": (2, 0), "Kiloava": (3, 0), "Akoya": (3, 1)},
    3: {"Deshret": (1, 0), "Balbala": (1, 1), "Asenath": (2, 0), "Nasima": (3, 0)},
    4: {"Venarius": (1, 0), "Maxarius": (1, 1), "Dominus": (2, 0), "Avarius": (3, 0)},
    5: {"Cadiro": (1, 0), "Victario": (2, 0), "Chitus": (3, 0), "Caspiro": (3, 1)},
    6: {"Vorana": (1, 0), "Uhtred": (2, 0), "Medved": (3, 0)},
}
# what the conqueror's fixed change to a small passive is: (text on an attribute passive, text elsewhere)
SMALL_ADDITION = {2: ("+2 to Strength", "+4 to Strength"), 3: ("+2 to Dexterity", "+4 to Dexterity"),
                  6: ("1% increased Ward", "2% increased Ward")}


def conqueror_name(version: int, keystone: int, revision: int) -> str | None:
    return next((n for n, kr in CONQUERORS.get(version, {}).items() if kr == (keystone, revision)), None)


def _number(s: str) -> int:
    return int(re.sub(r"\D", "", s))


def _loose(line: str) -> str:
    """A seed line as compared: no advanced-copy roll range ("2000(100-8000)" -> "2000"), ё read as е (pastes and
    keyboards often drop the dots), whitespace collapsed (non-breaking spaces included)."""
    line = re.sub(r"(\d)\s*\(\d+\s*[-–]\s*\d+\)", r"\1", line)
    return re.sub(r"\s+", " ", line.replace("ё", "е").replace("Ё", "Е")).strip()


def parse(text: str) -> dict | None:
    """{"version", "jewel", "seed", "conqueror" (English name), "lang"} from the jewel's seed line - the game's own
    wording in English, Russian or German - or None when the text has no seed line."""
    lines = [_loose(l) for l in text.replace("\r", "").split("\n") if l.strip()]
    if T.available():
        for lang, templates in T.conquerors().items():
            for t in templates:
                head, _, tail = _loose(t["line"]).partition("{1}")
                pat = re.compile(r"^" + re.escape(head.strip()) + r"\s*([\d\s., ]+?)\s*" + re.escape(tail.strip()) + r"$",
                                 re.I)
                for line in lines:
                    m = pat.match(line)
                    name = m and conqueror_name(t["version"], t["keystone"], t["revision"])
                    if name:
                        return {"version": t["version"], "jewel": T.JEWELS.get(t["version"]), "seed": _number(m.group(1)),
                                "conqueror": name, "lang": lang}
    for version, pat in ENGLISH_LINES.items():
        for line in lines:
            m = re.match(pat, line, re.I)
            if m:
                name = next((n for n in CONQUERORS[version] if n.lower() == m.group(2).lower()), None)
                if name:
                    return {"version": version, "jewel": T.JEWELS[version], "seed": int(m.group(1)), "conqueror": name,
                            "lang": "en"}
    return None


def valid_seed(version: int, seed: int) -> bool:
    lo, hi = T.SEEDS[version]
    div = T.SEED_DIVISOR.get(version, 1)
    return seed % div == 0 and lo <= seed // div <= hi


# ---------------------------------------------------------------- the tree

def _dist(a: dict, b: dict) -> float:
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def in_radius(socket_id: str) -> list[tuple[str, dict]]:
    tree = T.tree()
    s = tree["nodes"][str(socket_id)]
    return sorted(((nid, n) for nid, n in tree["nodes"].items()
                   if nid != str(socket_id) and n["kind"] not in ("socket", "mastery") and _dist(n, s) <= tree["radius"]),
                  key=lambda kv: _dist(kv[1], s))


def sockets() -> list[dict]:
    """Every socket a Timeless Jewel can go in: id, position, the class area it is in and the keystones / notables in
    its radius (what players recognise a socket by), ordered clockwise from the top."""
    tree = T.tree()
    out = []
    for sid in tree["sockets"]:
        s = tree["nodes"][sid]
        near = in_radius(sid)
        start = min(tree["starts"], key=lambda c: _dist(c, s)) if tree["starts"] else None
        out.append({"id": sid, "x": s["x"], "y": s["y"], "class": start["class"] if start else "",
                    "keystones": [n["name"] for _, n in near if n["kind"] == "keystone"],
                    "notables": [n["name"] for _, n in near if n["kind"] == "notable"],
                    "angle": math.degrees(math.atan2(s["x"], -s["y"])) % 360, "large": s["name"].startswith("Large")})
    return sorted(out, key=lambda s: s["angle"])


def tree_points() -> dict:
    """A light copy of the main tree for drawing the socket picker: [x, y, kind] per node, plus the sockets."""
    tree = T.tree()
    kinds = {"normal": 0, "notable": 1, "keystone": 2, "mastery": 3, "socket": 4}
    return {"points": [[n["x"], n["y"], kinds[n["kind"]]] for n in tree["nodes"].values()],
            "radius": tree["radius"], "sockets": [{"id": s["id"], "x": s["x"], "y": s["y"]} for s in sockets()]}


# ---------------------------------------------------------------- the effects

def _fmt(v) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _fill(line: str, key: str, st: dict, value) -> str:
    """Put a rolled value into a stat line (Path of Building's replaceHelperFunc): "(7-12)%" -> "9%"."""
    if value is None:
        return line
    if st.get("fmt") == "g":
        if "per_minute" in key:
            value = round(value / 60, 1)
        elif "permyriad" in key:
            value = value / 100
        elif "_ms" in key:
            value = value / 1000
    lo, hi = st.get("min"), st.get("max")
    if lo != hi:
        return line.replace(f"({_fmt(lo)}-{_fmt(hi)})", _fmt(value))
    if lo != value:
        return line.replace(_fmt(lo), _fmt(value))
    return line


def _rolled(p: dict, rolls: list) -> list[str]:
    """A replacement passive's lines with the rolls filled in (rolls[i] belongs to the stat with index i + 1)."""
    out = []
    for i, line in enumerate(p["sd"]):
        key = p["sorted"][i] if i < len(p["sorted"]) else None
        st = p["stats"].get(key, {}) if key else {}
        idx = st.get("index")
        out.append(_fill(line, key, st, rolls[idx - 1] if idx and idx - 1 < len(rolls) else None) if key else line)
    return out


def _addition(a: dict, value=None) -> list[str]:
    out = []
    for line in a["sd"]:
        for key, st in a["stats"].items():
            line = _fill(line, key, st, value)
        out.append(line)
    return out


def node_effect(version: int, seed: int, conqueror: str, nid: str, node: dict) -> dict:
    """{"id", "name", "kind", "old", "change": replaced / added / unchanged / missing, "new_name"?, "new"} for one
    passive in the radius."""
    lg = T.legion()
    ctype = T.CONQUEROR_TYPE[version]
    keystone, revision = CONQUERORS[version][conqueror]
    out = {"id": nid, "name": node["name"], "kind": node["kind"], "old": node["stats"], "change": "unchanged", "new": []}

    def replace(p, lines=None):
        out.update(change="replaced", new_name=p["name"], new=lines if lines is not None else list(p["sd"]))

    def add(lines):
        out.update(change="added", new=list(node["stats"]) + lines, added=lines)

    if node["kind"] == "keystone":
        want = f"{ctype}_keystone_{keystone}" + ("_v2" if revision else "")
        p = next((p for p in lg["nodes"] if p["id"] == want), None)
        if p:
            replace(p)
        else:
            out["change"] = "missing"
        return out
    attribute = node["name"] in ATTRIBUTE_NODES
    if node["kind"] == "normal" and version != 1:
        if version in SMALL_ADDITION:
            add([SMALL_ADDITION[version][0 if attribute else 1]])
        elif version == 4:                   # Templars: attribute passives become Devotion, the rest +5 Devotion
            if attribute:
                replace(next(p for p in lg["nodes"] if p["id"] == "templar_devotion_node"))
            else:
                add(["+5 to Devotion"])
        elif version == 5:                   # Eternal Empire: small passives are blanked
            replace(next(p for p in lg["nodes"] if p["id"] == "eternal_small_blank"), [])
        return out
    entry = T.read(version, seed, nid)
    if not entry:
        out["change"] = "missing"
        return out
    if version == 1:
        if len(entry) in (2, 3):
            replace(lg["nodes"][entry[0] - T.ADDITIONS], _rolled(lg["nodes"][entry[0] - T.ADDITIONS], entry[1:]))
        elif len(entry) in (6, 8):
            half = len(entry) // 2
            ids, rolls = entry[:half], entry[half:]
            # more offensive than defensive additions: Might of the Vaal, otherwise Legacy of the Vaal
            bias = sum(1 if v <= 21 else -1 for v in ids)
            name_p = lg["nodes"][76 if bias >= 0 else 77]
            summed: dict = {}
            for v, r in zip(ids, rolls):
                summed[v] = summed.get(v, 0) + r
            lines = [line for v, r in summed.items() for line in _addition(lg["additions"][v], r)]
            replace(name_p, lines)
        else:
            out["change"] = "missing"
        return out
    v = entry[0]
    if v >= T.ADDITIONS:
        replace(lg["nodes"][v - T.ADDITIONS])
    else:
        add(_addition(lg["additions"][v]))
    return out


def calculate(version: int, seed: int, conqueror: str, socket_id: str) -> dict:
    """Every passive in the socket's radius with its change: keystones and notables first, then small passives."""
    from ..data.timeless_status import require
    provenance = require()
    if version not in T.JEWELS or conqueror not in CONQUERORS.get(version, {}):
        raise ValueError(M("m.err.tj_conqueror", jewel=version, conqueror=conqueror))
    if not valid_seed(version, seed):
        lo, hi = T.SEEDS[version]
        div = T.SEED_DIVISOR.get(version, 1)
        raise ValueError(M("m.eval.timeless_seed", seed=seed, lo=lo * div, hi=hi * div))
    if str(socket_id) not in T.tree()["sockets"]:
        raise ValueError(M("m.err.tj_socket", socket=socket_id))
    order = {"keystone": 0, "notable": 1, "normal": 2}
    nodes = [node_effect(version, seed, conqueror, nid, n) for nid, n in in_radius(socket_id)]
    nodes.sort(key=lambda e: (order.get(e["kind"], 3), e["name"]))
    return {"socket": str(socket_id), "nodes": nodes,
            "counts": {k: sum(1 for e in nodes if e["kind"] == k) for k in order},
            "missing": sum(1 for e in nodes if e["change"] == "missing"),
            "provenance": provenance, "verification": "not computed" if any(e["change"] == "missing" for e in nodes) else provenance["compatibility"]}


# ---------------------------------------------------------------- equivalent seeds

MAX_EQUIVALENTS = 30          # seeds priced in one trade search ("at least one of" these seeds)


def _as_good(version: int, want: tuple, got: tuple) -> tuple[bool, bool]:
    """(same or better, strictly better) for one passive's table entries. Lethal Pride & co: the same addition /
    replacement (their values are fixed). Glorious Vanity: the same replacement passive with every roll at least as
    high, or the same set of Might / Legacy additions with each summed roll at least as high."""
    if version != 1 or len(want) not in (2, 3, 6, 8):
        return want == got, False
    if len(want) in (2, 3):
        if len(got) != len(want) or got[0] != want[0]:
            return False, False
        ok = all(g >= w for g, w in zip(got[1:], want[1:]))
        return ok, ok and got[1:] != want[1:]
    if len(got) not in (6, 8):
        return False, False

    def summed(e):
        half = len(e) // 2
        out: dict = {}
        for v, r in zip(e[:half], e[half:]):
            out[v] = out.get(v, 0) + r
        return out
    w, g = summed(want), summed(got)
    if set(w) != set(g):
        return False, False
    ok = all(g[k] >= w[k] for k in w)
    return ok, ok and g != w


def equivalents(version: int, seed: int, socket_id: str, node_ids: list[str] | None = None, limit: int = MAX_EQUIVALENTS) -> dict:
    """Other seeds of this jewel that change the must-have notables in the socket's radius the same way (or better).
    Only notables depend on the seed (keystones and the other jewels' small passives depend on the conqueror alone),
    so this compares the table entries of those notables across the whole seed range - one read per notable.
    {"core": [notable ids], "checked": seeds compared, "seeds": [{"seed", "matched", "of", "better"}] (exact matches
    first, then the closest), "exact": how many match every must-have}."""
    from ..data.timeless_status import require
    provenance = require()
    if not valid_seed(version, seed):
        lo, hi = T.SEEDS[version]
        div = T.SEED_DIVISOR.get(version, 1)
        raise ValueError(M("m.eval.timeless_seed", seed=seed, lo=lo * div, hi=hi * div))
    notables = [nid for nid, n in in_radius(socket_id) if n["kind"] == "notable"]
    core = [n for n in (node_ids or notables) if n in notables]
    lo, hi = T.SEEDS[version]
    div = T.SEED_DIVISOR.get(version, 1)
    own = seed // div - lo
    cols = []
    for nid in core:
        col = T.column(version, nid)
        if col is None:
            raise T.TimelessDataMissing("Missing table entry for notable " + str(nid))
        cols.append((nid, col))
    scores = []
    for off in range(hi - lo + 1):
        if off == own:
            continue
        matched = better = 0
        for _, col in cols:
            ok, up = _as_good(version, col[own], col[off])
            matched += ok
            better += up
        if matched:
            scores.append((matched, better, off))
    scores.sort(key=lambda x: (-x[0], -x[1], x[2]))
    out = [{"seed": (off + lo) * div, "matched": m, "of": len(cols), "better": b} for m, b, off in scores[:limit]]
    return {"core": [nid for nid, _ in cols], "checked": hi - lo, "seeds": out,
            "provenance": provenance, "verification": provenance["compatibility"],
            "exact": sum(1 for m, _, _ in scores if m == len(cols)) if cols else 0}


def trade_payload_seeds(info: dict, seeds: list[int], status: str = "online") -> dict:
    """One trade search for several seeds of this jewel and conqueror: "at least one of" these seed values."""
    stat = f"explicit.pseudo_timeless_jewel_{info['conqueror'].lower()}"
    return {"query": {"status": {"option": status}, "name": info["jewel"], "type": "Timeless Jewel",
                      "stats": [{"type": "count", "value": {"min": 1},
                                 "filters": [{"id": stat, "value": {"min": s, "max": s}} for s in seeds]}]},
            "sort": {"price": "asc"}}


def trade_payload_range(info: dict, lo: int, hi: int, status: str = "online") -> dict:
    """Every listed jewel of this name and conqueror, any seed in lo..hi, cheapest first."""
    stat = f"explicit.pseudo_timeless_jewel_{info['conqueror'].lower()}"
    return {"query": {"status": {"option": status}, "name": info["jewel"], "type": "Timeless Jewel",
                      "stats": [{"type": "and", "filters": [{"id": stat, "value": {"min": lo, "max": hi}}]}]},
            "sort": {"price": "asc"}}


def listing_seed(mods: list[str]) -> int | None:
    """The seed a listed jewel has, from its modifier text."""
    info = parse("\n".join(mods))
    return info["seed"] if info else None


def trade_payload(info: dict, status: str = "online") -> dict:
    """Trade search for this exact jewel: name, conqueror and seed (the site's pseudo stat per conqueror)."""
    stat = f"explicit.pseudo_timeless_jewel_{info['conqueror'].lower()}"
    return {"query": {"status": {"option": status}, "name": info["jewel"], "type": "Timeless Jewel",
                      "stats": [{"type": "and", "filters": [{"id": stat, "value": {"min": info["seed"],
                                                                                     "max": info["seed"]}}]}]},
            "sort": {"price": "asc"}}
