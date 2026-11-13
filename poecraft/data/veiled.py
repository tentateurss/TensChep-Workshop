"""Veiled modifiers (Betrayal / Syndicate) from the RePoE export.

The SQLite build only keeps rollable and crafted explicits, so this module reads the same raw RePoE files that
`python -m poecraft update` downloads (data/raw/mods.json and base_items.json), like the Eldritch module. A small
derived cache (data/veiled_cache.json) is rebuilt automatically whenever the raw file changes.

What the export provides (used as data):
  * every unveiled outcome (mod domain 'unveiled'): text, stats and values, mod groups, prefix/suffix, required
    level and item-tag spawn weights (first matching tag decides, as for explicits);
  * the veiled placeholders (domain 'veiled': VeiledPrefix / VeiledSuffix and Syndicate-member versions such as
    AislinVeiledSuffix__) - no text, no spawn weights;
  * the currency texts: Veiled Chaos Orb "Reforges a rare item with new random modifiers including a random Veiled
    modifier"; Veiled Exalted Orb "Removes a random modifier and adds a random Veiled modifier to a rare item".
What it does NOT provide (the planner marks these approximate / not supported):
  * Aisling's Research crafts (the safehouse rewards are not in the export at all);
  * how the three unveil options are drawn (the planner assumes 3 distinct outcomes drawn by spawn weight without
    replacement, excluding mod groups already on the item);
  * whether a random veiled modifier is a prefix or a suffix (assumed 50/50 for the Veiled Chaos Orb);
  * which Syndicate member's outcomes ("Catarina's", "It's", "Aisling's" ...) a veiled mod can unveil into - those
    need that member's own veiled mod and are not offered; only the generic 'Chosen' / 'of the Order' pool is used.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .repository import Mod, Stat

CACHE_PATH = config.DATA_DIR / "veiled_cache.json"
GENERIC_NAMES = ("Chosen", "of the Order")       # the unveil pool of an ordinary veiled prefix / suffix
CURRENCIES = ("Veiled Chaos Orb", "Veiled Exalted Orb")


@dataclass(frozen=True)
class UnveiledMod:
    mod: Mod                   # a normal explicit Mod (domain 'unveiled') once unveiled
    member: str | None         # None for the generic pool, else the Syndicate member's prefix/suffix name

    def weight(self, item_tags) -> int:
        return self.mod.spawn_weight(set(item_tags))


class VeiledData:
    def __init__(self, raw: dict):
        self.mods = [UnveiledMod(Mod(m["id"], m["name"], "unveiled", m["affix"], m["type"], m["required_level"],
                                     False, m["text"], tuple(m["groups"]), tuple(m["tags"]),
                                     tuple((t, w) for t, w in m["spawn_weights"]), (),
                                     tuple(Stat(s["id"], s["min"], s["max"]) for s in m["stats"])),
                                 None if m["name"] in GENERIC_NAMES else m["name"])
                     for m in raw["mods"]]
        self.currencies = dict(raw["currencies"])        # name -> description text


def _build(raw_dir: Path) -> dict:
    mods = json.loads((raw_dir / "mods.json").read_text(encoding="utf-8"))
    out = []
    for mid, m in mods.items():
        if m.get("domain") != "unveiled" or m.get("generation_type") not in ("prefix", "suffix") or not m.get("stats"):
            continue
        out.append({"id": mid, "name": m.get("name") or "", "affix": m["generation_type"],
                    "type": m.get("type", ""), "required_level": m.get("required_level", 1),
                    "text": m.get("text") or "", "groups": m.get("groups", []),
                    "tags": m.get("implicit_tags", []),
                    "spawn_weights": [(s["tag"], s["weight"]) for s in m.get("spawn_weights", [])],
                    "stats": m["stats"]})
    bases = json.loads((raw_dir / "base_items.json").read_text(encoding="utf-8"))
    currencies = []
    for b in bases.values():
        if b.get("item_class") == "StackableCurrency" and b.get("name") in CURRENCIES:
            desc = " ".join(((b.get("properties") or {}).get("description") or "").split())
            currencies.append((b["name"], desc))
    return {"mods": out, "currencies": currencies}


@lru_cache(maxsize=1)
def get_veiled(raw_dir: Path = config.RAW_DIR) -> VeiledData:
    src = raw_dir / "mods.json"
    if not src.exists():
        raise FileNotFoundError(f"{src} missing - run `python -m poecraft update`")
    stamp = {"size": src.stat().st_size, "mtime": src.stat().st_mtime}
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("source") == stamp:
            return VeiledData(cached["data"])
    except (OSError, ValueError):
        pass
    data = _build(raw_dir)
    try:
        CACHE_PATH.write_text(json.dumps({"source": stamp, "data": data}), encoding="utf-8")
    except OSError:
        pass                                   # the cache is an optimisation only
    return VeiledData(data)
