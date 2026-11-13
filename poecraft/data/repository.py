"""Read-only access to the SQLite game database as plain Python objects."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .. import config
from ..messages import M


@dataclass(frozen=True)
class Stat:
    id: str
    min: float
    max: float


@dataclass(frozen=True)
class Mod:
    id: str
    name: str
    domain: str              # "item" (rollable) or "crafted" (bench)
    affix: str               # "prefix" | "suffix"
    type: str
    required_level: int
    essence_only: bool
    text: str
    groups: tuple[str, ...]
    tags: tuple[str, ...]    # implicit_tags: what fossils / metacrafts / harvest look at
    spawn_weights: tuple[tuple[str, int], ...]
    generation_weights: tuple[tuple[str, int], ...]
    stats: tuple[Stat, ...]

    @property
    def group(self) -> str:
        return self.groups[0] if self.groups else self.id

    @property
    def signature(self) -> str:
        """Identity of 'the same stat line' across regular, essence, influenced and crafted mods."""
        return self.affix + "|" + ",".join(sorted(s.id for s in self.stats))

    def spawn_weight(self, item_tags: set[str]) -> int:
        """PoE rule: the first spawn tag present on the item decides the weight."""
        return self.spawn_tag_weight(item_tags)[1]

    def spawn_tag_weight(self, item_tags: set[str]) -> tuple[str | None, int]:
        for tag, w in self.spawn_weights:
            if tag in item_tags:
                return tag, w
        return None, 0

    def generation_multiplier(self, item_tags: set[str]) -> float:
        for tag, w in self.generation_weights:
            if tag in item_tags:
                return w / 100.0
        return 1.0


@dataclass(frozen=True)
class Base:
    id: str
    name: str
    item_class: str
    drop_level: int
    tags: tuple[str, ...]
    implicits: tuple[str, ...]
    properties: dict = field(hash=False, compare=False)
    requirements: dict = field(hash=False, compare=False)


@dataclass(frozen=True)
class Essence:
    id: str
    name: str
    level: int
    tier: int
    corruption_only: bool
    mods: dict = field(hash=False, compare=False)   # item_class -> mod_id
    # essences.json "item_level_restriction": the essence cannot be used on items above this item level
    # (Whispering 35, Muttering 45, Weeping 60, Wailing 75; None = no restriction)
    item_level_restriction: int | None = None


@dataclass(frozen=True)
class Fossil:
    id: str
    name: str
    weights: tuple[tuple[str, int], ...]      # positive + negative, tag -> weight (100 = x1)
    added_mods: tuple[str, ...]
    forced_mods: tuple[str, ...]
    allowed_tags: tuple[str, ...]
    forbidden_tags: tuple[str, ...]


@dataclass(frozen=True)
class BenchCraft:
    id: int
    mod_id: str
    master: str
    item_classes: frozenset[str]
    cost: tuple[tuple[str, int], ...]         # currency name -> count


class GameData:
    def __init__(self, db_path: Path = config.DB_PATH):
        if not db_path.exists():
            raise FileNotFoundError(
                f"Database {db_path} not found. Run `python -m poecraft update` to download and build it.")
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        self.meta = {r["key"]: r["value"] for r in con.execute("SELECT * FROM meta")}
        self.currency_names = {r["id"]: r["name"] for r in con.execute("SELECT * FROM currencies")}
        self.influence_tags = {
            r["id"]: tuple(json.loads(r["influence_tags"])) for r in con.execute("SELECT * FROM item_classes")
        }
        self.bases: dict[str, Base] = {}
        for r in con.execute("SELECT * FROM bases ORDER BY item_class, drop_level"):
            self.bases[r["id"]] = Base(
                r["id"], r["name"], r["item_class"], r["drop_level"], tuple(json.loads(r["tags"])),
                tuple(json.loads(r["implicits"])), json.loads(r["properties"]), json.loads(r["requirements"]))
        self.mods: dict[str, Mod] = {}
        for r in con.execute("SELECT * FROM mods"):
            self.mods[r["id"]] = Mod(
                r["id"], r["name"], r["domain"], r["generation_type"], r["type"], r["required_level"],
                bool(r["is_essence_only"]), r["text"], tuple(json.loads(r["groups"])),
                tuple(json.loads(r["implicit_tags"])),
                tuple((x["tag"], x["weight"]) for x in json.loads(r["spawn_weights"])),
                tuple((x["tag"], x["weight"]) for x in json.loads(r["generation_weights"])),
                tuple(Stat(s["id"], s["min"], s["max"]) for s in json.loads(r["stats"])),
            )
        ess_mods: dict[str, dict] = {}
        for r in con.execute("SELECT * FROM essence_mods"):
            ess_mods.setdefault(r["essence_id"], {})[r["item_class"]] = r["mod_id"]
        self.essences = [
            Essence(r["id"], r["name"], r["level"], r["tier"], bool(r["corruption_only"]), ess_mods.get(r["id"], {}),
                    r["item_level_restriction"])
            for r in con.execute("SELECT * FROM essences ORDER BY level")
        ]
        self.fossils = []
        for r in con.execute("SELECT * FROM fossils"):
            weights = [(x["tag"], x["weight"]) for x in json.loads(r["positive_weights"])]
            weights += [(x["tag"], x["weight"]) for x in json.loads(r["negative_weights"])]
            self.fossils.append(Fossil(
                r["id"], r["name"], tuple(weights), tuple(json.loads(r["added_mods"])),
                tuple(json.loads(r["forced_mods"])), tuple(json.loads(r["allowed_tags"])),
                tuple(json.loads(r["forbidden_tags"]))))
        self.bench_crafts = []
        for r in con.execute("SELECT * FROM bench_crafts"):
            cost = tuple((self.currency_names.get(k, k.rsplit("/", 1)[-1]), v)
                         for k, v in json.loads(r["cost"]).items())
            self.bench_crafts.append(BenchCraft(
                r["id"], r["mod_id"], r["master"], frozenset(json.loads(r["item_classes"])), cost))
        con.close()

    def find_bases(self, query: str = "", item_class: str | None = None, limit: int = 60) -> list[Base]:
        q = query.lower().strip()
        out = [b for b in self.bases.values()
               if (not q or q in b.name.lower()) and (not item_class or b.item_class == item_class)]
        out.sort(key=lambda b: (not b.name.lower().startswith(q), -b.drop_level, b.name))
        return out[:limit]

    def base_by_name(self, name: str) -> Base:
        exact = [b for b in self.bases.values() if b.name.lower() == name.lower()]
        if exact:
            return max(exact, key=lambda b: b.drop_level)
        found = self.find_bases(name, limit=1)
        if not found:
            raise KeyError(M("m.err.no_base", name=name))
        return found[0]

    def fossil_by_name(self, name: str) -> Fossil:
        for f in self.fossils:
            if f.name.lower() == name.lower():
                return f
        raise KeyError(name)


@lru_cache(maxsize=1)
def get_data() -> GameData:
    return GameData()


@lru_cache(maxsize=1)
def get_rules() -> dict:
    return json.loads(config.RULES_PATH.read_text(encoding="utf-8"))
