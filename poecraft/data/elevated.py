"""Elevated influenced modifiers (Orb of Dominance outcomes) from the RePoE export.

The SQLite build only keeps rollable and crafted explicits, so this module reads the same raw RePoE files that
`python -m poecraft update` downloads (data/raw/mods.json and base_items.json), like the Eldritch module. A small
derived cache (data/elevated_cache.json) is rebuilt automatically whenever the raw file changes.

What the export provides (used as data):
  * every Elevated modifier (name "Elevated Shaper's" ... "Elevated Warlord's"): text, stats, values, groups,
    prefix/suffix, required level - and NO spawn weights (they only come from the Orb of Dominance);
  * the Orb of Dominance text: "Removes one Influenced Modifier from an item with at least two Influenced Modifiers
    and upgrades another Influenced Modifier. Upgrading a modifier of the highest tier transforms the modifier into
    an Elevated Modifier ... Can be used on Body Armours, Boots, Gloves and Helmets".
What it does NOT provide:
  * which influenced modifier is removed and which is upgraded (no odds at all);
  * an explicit link from an Elevated modifier to the influenced modifier it upgrades from. The link is derived
    here: same influence, same side, a shared mod group and the influenced mod's stats contained in the Elevated
    mod's stats. When that matches exactly one influenced mod type, it is used; otherwise the Elevated mod is left
    unlinked (and reported as "not supported (missing outcome data)").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .repository import Mod, Stat, get_rules

CACHE_PATH = config.DATA_DIR / "elevated_cache.json"
ORB = "Orb of Dominance"
_CLASSES = re.compile(r"Can be used on (.+?)$")
_WORD = {"Shaper": "shaper", "Elder": "elder", "Crusader": "crusader", "Hunter": "hunter", "Redeemer": "redeemer",
         "Warlord": "warlord"}
# class names in the currency text -> item_class values of the export
_CLASS_NAMES = {"Body Armours": "Body Armour", "Boots": "Boots", "Gloves": "Gloves", "Helmets": "Helmet"}


@dataclass(frozen=True)
class ElevatedMod:
    mod: Mod
    influence: str              # "shaper" ... "warlord"
    base_type: str | None       # mod type of the influenced modifier it upgrades from (None: no unique link)


class ElevatedData:
    def __init__(self, raw: dict):
        self.mods = [ElevatedMod(Mod(m["id"], m["name"], "item", m["affix"], m["type"], m["required_level"], False,
                                     m["text"], tuple(m["groups"]), tuple(m["tags"]), (), (),
                                     tuple(Stat(s["id"], s["min"], s["max"]) for s in m["stats"])),
                                 m["influence"], m["base_type"]) for m in raw["mods"]]
        self.orb_text = raw["orb_text"]
        self.classes = tuple(raw["classes"])          # item classes the Orb of Dominance can be used on


def _build(raw_dir: Path) -> dict:
    mods = json.loads((raw_dir / "mods.json").read_text(encoding="utf-8"))
    suffix = get_rules()["influences"]                # influence -> spawn-tag suffix, e.g. hunter -> _basilisk
    infl: dict[str, list] = {}
    for m in mods.values():
        if m.get("domain") != "item" or m.get("generation_type") not in ("prefix", "suffix"):
            continue
        if (m.get("name") or "").startswith("Elevated "):
            continue
        for inf, suf in suffix.items():
            if any(s["tag"].endswith(suf) and s["weight"] > 0 for s in m.get("spawn_weights", [])):
                infl.setdefault(inf, []).append(m)
    out = []
    for mid, m in mods.items():
        name = m.get("name") or ""
        if not name.startswith("Elevated ") or m.get("generation_type") not in ("prefix", "suffix") or not m.get("stats"):
            continue
        inf = _WORD.get(name.split()[1].replace("'s", ""))
        if inf is None:
            continue
        stats = {s["id"] for s in m["stats"]}
        types = {b["type"] for b in infl.get(inf, [])
                 if b["generation_type"] == m["generation_type"] and set(b.get("groups", [])) & set(m.get("groups", []))
                 and {s["id"] for s in b.get("stats", [])} <= stats}
        out.append({"id": mid, "name": name, "influence": inf, "affix": m["generation_type"], "type": m.get("type", ""),
                    "required_level": m.get("required_level", 1), "text": m.get("text") or "",
                    "groups": m.get("groups", []), "tags": m.get("implicit_tags", []), "stats": m["stats"],
                    "base_type": next(iter(types)) if len(types) == 1 else None})
    bases = json.loads((raw_dir / "base_items.json").read_text(encoding="utf-8"))
    text, classes = "", []
    for b in bases.values():
        if b.get("item_class") == "StackableCurrency" and b.get("name") == ORB:
            text = " ".join(((b.get("properties") or {}).get("description") or "").split())
            hit = _CLASSES.search(text)
            if hit:
                names = [c.strip() for c in re.split(r", | and ", hit.group(1)) if c.strip()]
                classes = [_CLASS_NAMES.get(c, c) for c in names]
    return {"mods": out, "orb_text": text, "classes": classes}


@lru_cache(maxsize=1)
def get_elevated(raw_dir: Path = config.RAW_DIR) -> ElevatedData:
    src = raw_dir / "mods.json"
    if not src.exists():
        raise FileNotFoundError(f"{src} missing - run `python -m poecraft update`")
    stamp = {"size": src.stat().st_size, "mtime": src.stat().st_mtime}
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("source") == stamp:
            return ElevatedData(cached["data"])
    except (OSError, ValueError):
        pass
    data = _build(raw_dir)
    try:
        CACHE_PATH.write_text(json.dumps({"source": stamp, "data": data}), encoding="utf-8")
    except OSError:
        pass                                   # the cache is an optimisation only
    return ElevatedData(data)
