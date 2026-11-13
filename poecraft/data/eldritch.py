"""Eldritch implicits (Searing Exarch / Eater of Worlds) from the RePoE export.

The SQLite build only keeps explicit prefixes/suffixes, so this module reads the same raw RePoE files that
`python -m poecraft update` downloads (data/raw/mods.json and base_items.json). A small derived cache
(data/eldritch_cache.json) avoids re-parsing the 20 MB mods file on every run and is rebuilt automatically
whenever the raw file changes, so a new league needs nothing beyond `update`.

What the export provides (used as data):
  * every Eldritch implicit: stats, values, mod group, item-tag spawn weights, required level;
  * its tier: variant k carries the spawn tag `no_tier_{7-k}_eldritch_implicit` (weight 0), so k=1 is tier 6
    (weakest) ... k=6 is tier 1 (strongest);
  * the Ember / Ichor descriptions, e.g. "Adds a Grand Searing Exarch implicit modifier to a Body Armour,
    Boots, Gloves or Helmet" and "Cannot be used on Shaper, Elder or Elderslayer influenced items".
What it does NOT provide (so the planner marks these parts approximate / not supported):
  * a table linking each currency to the tier number it creates - taken from the tier word in the description
    (rules 'eldritch_tier_names': Lesser=6, Greater=5, Grand=4, Exceptional=3; 2 and 1 only via Orb of Conflict);
  * when the higher-valued 'UniquePresence' / 'PinnaclePresence' versions apply (only under an Eldritch
    presence) - they are left out, i.e. crafting is assumed to happen without a presence.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .repository import Stat

_TIER_TAG = re.compile(r"no_tier_(\d)_eldritch_implicit")
_DESC = re.compile(r"Adds an? (\w+) (Searing Exarch|Eater of Worlds) implicit modifier to (.+?) This replaces")
SIDES = {"searing_exarch_implicit": "exarch", "eater_of_worlds_implicit": "eater"}
SIDE_NAMES = {"exarch": "Searing Exarch", "eater": "Eater of Worlds"}
CACHE_PATH = config.DATA_DIR / "eldritch_cache.json"


@dataclass(frozen=True)
class EldritchMod:
    id: str
    side: str                 # "exarch" | "eater"
    type: str
    tier: int                 # 1 (best) .. 6 (weakest)
    text: str
    groups: tuple[str, ...]
    stats: tuple[Stat, ...]
    spawn_weights: tuple[tuple[str, int], ...]
    required_level: int

    @property
    def key(self) -> str:
        """Identity of an Eldritch stat line (side included: the same stat can exist on both sides)."""
        return self.side + "|" + ",".join(sorted(s.id for s in self.stats))

    def weight(self, item_tags) -> int:
        for tag, w in self.spawn_weights:      # first tag present on the item decides, as for explicits
            if tag in item_tags:
                return w
        return 0


@dataclass(frozen=True)
class EldritchCurrency:
    name: str
    side: str
    tier_name: str            # "Lesser" | "Greater" | "Grand" | "Exceptional"
    item_classes: tuple[str, ...]
    directions: str


class EldritchData:
    def __init__(self, raw: dict):
        self.mods = [EldritchMod(m["id"], m["side"], m["type"], m["tier"], m["text"], tuple(m["groups"]),
                                 tuple(Stat(s["id"], s["min"], s["max"]) for s in m["stats"]),
                                 tuple((t, w) for t, w in m["spawn_weights"]), m["required_level"])
                     for m in raw["mods"]]
        self.currencies = [EldritchCurrency(c["name"], c["side"], c["tier_name"], tuple(c["classes"]),
                                            c["directions"]) for c in raw["currencies"]]

    def classes(self, side: str) -> tuple[str, ...]:
        out = []
        for c in self.currencies:
            if c.side == side:
                out += [x for x in c.item_classes if x not in out]
        return tuple(out)


def _build(raw_dir: Path) -> dict:
    mods = json.loads((raw_dir / "mods.json").read_text(encoding="utf-8"))
    out = []
    for mid, m in mods.items():
        side = SIDES.get(m.get("generation_type"))
        if not side or "Presence" in (m.get("type") or ""):
            continue                           # presence versions: gating not in the export
        tier = next((int(t.group(1)) for s in m.get("spawn_weights", []) for t in [_TIER_TAG.fullmatch(s["tag"])]
                     if t), None)
        if tier is None or not m.get("stats"):
            continue
        out.append({"id": mid, "side": side, "type": m.get("type", ""), "tier": tier, "text": m.get("text") or "",
                    "groups": m.get("groups", []), "stats": m["stats"],
                    "spawn_weights": [(s["tag"], s["weight"]) for s in m["spawn_weights"]],
                    "required_level": m.get("required_level", 1)})
    bases = json.loads((raw_dir / "base_items.json").read_text(encoding="utf-8"))
    currencies = []
    for b in bases.values():
        name = b.get("name") or ""
        if b.get("item_class") != "StackableCurrency" or not ("Eldritch Ember" in name or "Eldritch Ichor" in name):
            continue
        props = b.get("properties") or {}
        desc = " ".join((props.get("description") or "").split())
        m = _DESC.search(desc)
        if not m:
            continue
        target = re.sub(r"^an? ", "", m.group(3))
        classes = [c.strip() for c in re.split(r", | or ", target) if c.strip()]
        currencies.append({"name": name, "side": "exarch" if m.group(2) == "Searing Exarch" else "eater",
                           "tier_name": m.group(1), "classes": classes,
                           "directions": " ".join((props.get("directions") or "").split())})
    return {"mods": out, "currencies": currencies}


@lru_cache(maxsize=1)
def get_eldritch(raw_dir: Path = config.RAW_DIR) -> EldritchData:
    src = raw_dir / "mods.json"
    if not src.exists():
        raise FileNotFoundError(f"{src} missing - run `python -m poecraft update`")
    stamp = {"size": src.stat().st_size, "mtime": src.stat().st_mtime}
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("source") == stamp:
            return EldritchData(cached["data"])
    except (OSError, ValueError):
        pass
    data = _build(raw_dir)
    try:
        CACHE_PATH.write_text(json.dumps({"source": stamp, "data": data}), encoding="utf-8")
    except OSError:
        pass                                   # the cache is an optimisation only
    return EldritchData(data)
