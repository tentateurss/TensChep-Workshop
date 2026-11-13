"""Special fossils and Beastcrafting from the RePoE export.

The SQLite build keeps fossil weights and mod ids but not the special fossils' own modifiers (domain 'delve') or
their effect descriptions, so this module reads the same raw RePoE files that `python -m poecraft update`
downloads (data/raw/fossils.json and mods.json), like the Eldritch module. A small derived cache
(data/special_cache.json) is rebuilt automatically whenever the raw files change.

What the export provides (used as data):
  * per fossil: tag weights, 'added_mods', 'forced_mods', allowed / forbidden item tags, 'corrupted_essence_chance',
    'rolls_lucky', 'sell_price_mods' and effect descriptions (e.g. Hollow: "Has an Abyssal socket");
  * the special fossils' modifiers themselves (e.g. DelveAbyssJewelSocket1 "Has 1 Abyssal Socket", suffix).
What it does NOT provide:
  * any Beastcrafting recipe: no file of the export lists recipes, the beasts they need, their cost or what they
    add. Only the outcome modifiers of some recipes exist in mods.json (the Aspect suffixes 'of Farrul',
    'of Saqawal', 'of Fenumus', 'of Craiceann'), with no spawn weights and nothing linking them to a recipe - they
    are identified here by those names (rules 'beastcraft_outcome_names'), not by any beastcraft label in the data;
  * the parts of some fossil effects (see rules 'fossil_effects'), e.g. Tangled's random weights.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import config
from .repository import Mod, Stat, get_rules

CACHE_PATH = config.DATA_DIR / "special_cache.json"


def _mod(m: dict) -> Mod:
    return Mod(m["id"], m["name"], m["domain"], m["affix"], m["type"], m["required_level"], False, m["text"],
               tuple(m["groups"]), tuple(m["tags"]), tuple((t, w) for t, w in m["spawn_weights"]), (),
               tuple(Stat(s["id"], s["min"], s["max"]) for s in m["stats"]))


@dataclass(frozen=True)
class SpecialFossil:
    name: str
    weights: tuple[tuple[str, int], ...]
    added: tuple[Mod, ...]
    forced: tuple[Mod, ...]
    allowed_tags: tuple[str, ...]
    forbidden_tags: tuple[str, ...]
    descriptions: tuple[tuple[str, str], ...]      # effect key -> text, as in the export
    corrupted_essence_chance: int
    rolls_lucky: bool
    sell_price_mods: tuple[str, ...]


class SpecialData:
    def __init__(self, raw: dict):
        self.fossils = {f["name"]: SpecialFossil(
            f["name"], tuple((t, w) for t, w in f["weights"]), tuple(_mod(m) for m in f["added"]),
            tuple(_mod(m) for m in f["forced"]), tuple(f["allowed_tags"]), tuple(f["forbidden_tags"]),
            tuple((k, v) for k, v in f["descriptions"]), f["corrupted_essence_chance"], f["rolls_lucky"],
            tuple(f["sell_price_mods"])) for f in raw["fossils"]}
        self.beast_outcomes = tuple(_mod(m) for m in raw["beast_outcomes"])
        self.recipes = tuple(raw["recipes"])         # always empty with the current export (kept for honesty)


def _raw_mod(mid: str, m: dict) -> dict:
    return {"id": mid, "name": m.get("name") or "", "domain": m.get("domain", ""),
            "affix": m.get("generation_type", ""), "type": m.get("type", ""),
            "required_level": m.get("required_level", 1), "text": m.get("text") or "",
            "groups": m.get("groups", []), "tags": m.get("implicit_tags", []),
            "spawn_weights": [(s["tag"], s["weight"]) for s in m.get("spawn_weights", [])],
            "stats": m.get("stats", [])}


def _build(raw_dir: Path) -> dict:
    rules = get_rules()
    mods = json.loads((raw_dir / "mods.json").read_text(encoding="utf-8"))
    fossils = json.loads((raw_dir / "fossils.json").read_text(encoding="utf-8"))
    names = set(rules["fossils_not_modelled"])
    out = []
    for f in fossils.values():
        if f.get("name") not in names:
            continue
        out.append({
            "name": f["name"],
            "weights": [(w["tag"], w["weight"]) for w in f.get("positive_mod_weights", []) + f.get("negative_mod_weights", [])],
            "added": [_raw_mod(mid, mods[mid]) for mid in f.get("added_mods", []) if mid in mods and mods[mid].get("stats")],
            "forced": [_raw_mod(mid, mods[mid]) for mid in f.get("forced_mods", []) if mid in mods and mods[mid].get("stats")],
            "allowed_tags": f.get("allowed_tags", []), "forbidden_tags": f.get("forbidden_tags", []),
            "descriptions": [(k, " ".join(v.split())) for k, v in (f.get("descriptions") or {}).items()],
            "corrupted_essence_chance": f.get("corrupted_essence_chance", 0), "rolls_lucky": bool(f.get("rolls_lucky")),
            "sell_price_mods": f.get("sell_price_mods", []),
        })
    beast_names = set(rules["beastcraft_outcome_names"])
    beasts = [_raw_mod(mid, m) for mid, m in mods.items()
              if (m.get("name") or "") in beast_names and m.get("domain") == "item" and m.get("stats")]
    return {"fossils": out, "beast_outcomes": beasts, "recipes": []}


@lru_cache(maxsize=1)
def get_special(raw_dir: Path = config.RAW_DIR) -> SpecialData:
    srcs = [raw_dir / "mods.json", raw_dir / "fossils.json"]
    for src in srcs:
        if not src.exists():
            raise FileNotFoundError(f"{src} missing - run `python -m poecraft update`")
    stamp = [{"size": s.stat().st_size, "mtime": s.stat().st_mtime} for s in srcs]
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("source") == stamp:
            return SpecialData(cached["data"])
    except (OSError, ValueError):
        pass
    data = _build(raw_dir)
    try:
        CACHE_PATH.write_text(json.dumps({"source": stamp, "data": data}), encoding="utf-8")
    except OSError:
        pass                                   # the cache is an optimisation only
    return SpecialData(data)
