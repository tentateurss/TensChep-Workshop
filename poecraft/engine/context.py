"""Item context: which mods can roll on a given base / item level / influence set, with what weight."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from ..data.repository import BenchCraft, Base, Essence, Fossil, GameData, Mod

_RANGE = re.compile(r"\(-?[\d.]+--?[\d.]+\)")
_NUMBER = re.compile(r"(?<![\w#])-?\d+(?:\.\d+)?")


def tag_allowed(restrict: str | None, tags) -> bool:
    """Pool filter: None = everything, 'fire' = only mods tagged fire, '!attack' = no attack mods."""
    if restrict is None:
        return True
    if restrict.startswith("!"):
        return restrict[1:] not in tags
    return restrict in tags


def event_only(mod: Mod, rules: dict) -> bool:
    """A mod of a temporary event mode (rules "event_only_mod_markers", e.g. Battle Royale): never in a crafting pool."""
    return any(marker in mod.id for marker in rules.get("event_only_mod_markers", ()))


# ------------------------------------------------------------------ displayed vs stored units
# The game stores some stats in other units than it shows: leech per 10 000 ("0.2%" = 20), regeneration per minute
# shown per second, durations in ms, distances in tenths of a metre ("1.2 m" = 12), spell suppression x1.5.
# Thresholds are typed and shown in DISPLAYED units and compared in STORED units. The factor of a stat
# (displayed = factor x stored) is read from the game's own mod texts: only when every mod with that primary stat
# shows the same positive factor at both ends of its range, over at least two different stored values. Anything
# else (no factor, sign flips of "reduced" mods, texts whose first number is another value) keeps factor 1.
_SCALES: dict | None = None
_DISPLAY_FIRST = re.compile(r"\((-?[\d.]+)-(-?[\d.]+)\)|(-?\d+(?:\.\d+)?)")


def _display_bounds(text: str):
    m = _DISPLAY_FIRST.search(text or "")
    if not m:
        return None
    if m.group(1) is not None:
        return float(m.group(1)), float(m.group(2))
    return float(m.group(3)), float(m.group(3))


def _build_scales() -> dict:
    from ..data.repository import get_data
    mods = [m for m in get_data().mods.values() if m.stats]
    try:
        from ..data.eldritch import get_eldritch
        eld = get_eldritch().mods
    except (OSError, ValueError, KeyError):       # no Eldritch data built: those stats simply get no factor
        eld = []
    mods += [m for m in (eld.values() if isinstance(eld, dict) else eld) if m.stats]
    factors: dict = {}
    values: dict = {}
    bad: set = set()
    for m in mods:
        s, b = m.stats[0], _display_bounds(m.text)
        if b is None or not s.min or not s.max:
            bad.add(s.id)
            continue
        lo, hi = b[0] / s.min, b[1] / s.max
        if lo <= 0 or abs(lo - hi) > 0.02 * hi:
            bad.add(s.id)
            continue
        factors.setdefault(s.id, []).append(hi)
        values.setdefault(s.id, set()).update((s.min, s.max))
    out = {}
    for sid, fs in factors.items():
        f = sorted(fs)[len(fs) // 2]
        if sid in bad or len(values[sid]) < 2 or any(abs(x - f) > 0.02 * f for x in fs) or abs(f - 1) < 1e-9:
            continue
        out[sid] = f
    return out


def stat_scale(stat_id: str | None) -> float | None:
    """displayed = factor x stored for this stat, or None (same units)."""
    global _SCALES
    if _SCALES is None:
        _SCALES = _build_scales()
    return _SCALES.get(stat_id) if stat_id else None


def to_display(stat_id: str | None, value):
    f = stat_scale(stat_id)
    return value if f is None or value is None else round(value * f, 6)


def to_stored(stat_id: str | None, value):
    f = stat_scale(stat_id)
    return value if f is None or value is None else value / f


def generic_text(text: str) -> str:
    """'+(80-89) to maximum Life' -> '+# to maximum Life'."""
    return _NUMBER.sub("#", _RANGE.sub("#", text)).replace("\n", " / ")


@dataclass(frozen=True)
class PoolEntry:
    mod: Mod
    weight: float
    tier: int


@dataclass
class Target:
    """'An explicit mod with this stat line, whose primary stat can be >= min_value'."""
    index: int
    signature: str
    stat_id: str
    min_value: float | None
    label: str
    affix: str

    def stat(self, mod: Mod):
        return next((s for s in mod.stats if s.id == self.stat_id), None)

    def satisfied_by(self, mod: Mod) -> bool:
        if mod.signature != self.signature:
            return False
        if self.min_value is None:
            return True
        s = self.stat(mod)
        return s is not None and s.max >= self.min_value

    def value_chance(self, mod: Mod) -> float:
        """Chance that a freshly rolled (or divined) value of this mod meets the threshold."""
        s = self.stat(mod)
        if s is None or self.min_value is None:
            return 1.0
        if s.max < self.min_value:
            return 0.0
        if self.min_value <= s.min:
            return 1.0
        if float(s.min).is_integer() and float(s.max).is_integer():
            return (s.max - math.ceil(self.min_value) + 1) / (s.max - s.min + 1)
        return (s.max - self.min_value) / (s.max - s.min)

    @property
    def shown_min(self):
        """The threshold in the units the game displays (min_value is in stored units)."""
        return to_display(self.stat_id, self.min_value)

    def describe(self) -> str:
        if self.min_value is None:
            return self.label
        return f"{self.label} (>= {self.shown_min:g})"


@dataclass
class Family:
    signature: str
    affix: str
    label: str
    primary_stat: str
    influence: bool = False
    tiers: list = field(default_factory=list)   # dicts, best first
    sources: set = field(default_factory=set)

    def to_json(self) -> dict:
        maxima = [t["max"] for t in self.tiers if t["max"] is not None]
        minima = [t["min"] for t in self.tiers if t["min"] is not None]
        return {
            "signature": self.signature, "affix": self.affix, "label": self.label,
            "primary_stat": self.primary_stat, "influence": self.influence,
            "sources": sorted(self.sources), "tiers": self.tiers,
            "max_value": max(maxima) if maxima else None, "min_value": min(minima) if minima else None,
            "roll_weight": sum(t["weight"] for t in self.tiers if t["source"] == "roll"),
        }


class ItemContext:
    def __init__(self, data: GameData, rules: dict, base: Base, ilvl: int, influences=()):
        self.data, self.rules, self.base, self.ilvl = data, rules, base, ilvl
        self.influences = tuple(influences)
        if len(self.influences) > rules["max_influences"]:
            raise ValueError(M("m.err.max_influences", n=rules["max_influences"]))
        self.tags = set(base.tags)
        for inf in self.influences:
            suffix = rules["influences"].get(inf)
            if suffix is None:
                raise ValueError(M("m.err.unknown_influence", name=inf))
            tag = next((t for t in data.influence_tags.get(base.item_class, ()) if t.endswith(suffix)), None)
            if tag:
                self.tags.add(tag)
        self.limits = dict(rules["affix_limits"])
        self.magic_limits = dict(rules["magic_affix_limits"])

        influence_tags = set(data.influence_tags.get(base.item_class, ()))
        self.influenced: set[str] = set()   # mod ids that roll only because of an influence
        spawnable = []
        for m in data.mods.values():
            if m.domain != "item" or m.essence_only or event_only(m, rules):
                continue
            tag, w = m.spawn_tag_weight(self.tags)
            w *= m.generation_multiplier(self.tags)
            if w > 0:
                spawnable.append((m, w))
                if tag in influence_tags:
                    self.influenced.add(m.id)
        # Tiers are ranked over every mod of a stat line that can roll on this base (ignoring ilvl).
        by_sig: dict[str, list] = {}
        for m, w in spawnable:
            by_sig.setdefault(m.signature, []).append(m)
        self.tier_of: dict[str, int] = {}
        for mods in by_sig.values():
            mods.sort(key=lambda m: (m.required_level, m.stats[0].max if m.stats else 0), reverse=True)
            for i, m in enumerate(mods):
                self.tier_of[m.id] = i + 1
        self.base_entries = [PoolEntry(m, w, self.tier_of[m.id]) for m, w in spawnable if m.required_level <= ilvl]

        cls = base.item_class
        usable = [e for e in data.essences if cls in e.mods and e.mods[cls] in data.mods]
        # an essence with an item level restriction cannot be used on a higher-level item (game data)
        self.essence_options: list[tuple[Essence, Mod]] = [
            (e, data.mods[e.mods[cls]]) for e in usable
            if e.item_level_restriction is None or ilvl <= e.item_level_restriction]
        self.essences_over_ilvl: list[tuple[Essence, Mod]] = [
            (e, data.mods[e.mods[cls]]) for e in usable
            if e.item_level_restriction is not None and ilvl > e.item_level_restriction]
        self.bench_options: list[tuple[BenchCraft, Mod]] = [
            (b, data.mods[b.mod_id]) for b in data.bench_crafts
            if cls in b.item_classes and b.mod_id in data.mods and self._bench_allowed(data.mods[b.mod_id])
        ]

    _LOCAL_DEFENCE = (("armour", "armour"), ("physical_damage_reduction", "armour"),
                      ("evasion", "evasion"), ("energy_shield", "energy_shield"))

    @classmethod
    def bench_inferred(cls, m: Mod) -> bool:
        """True when this craft's eligibility for the base came from the local-defence rule below, not data."""
        return any(s.id.startswith("local_") and any(key in s.id for key, _ in cls._LOCAL_DEFENCE) for s in m.stats)

    def _bench_allowed(self, m: Mod) -> bool:
        """Bench crafts carry no spawn tags in the export; apply the in-game restrictions we know."""
        if not m.stats or any(s.id.startswith("item_generation") for s in m.stats):
            return False   # metacrafts are planner tools, not target stats
        for s in m.stats:
            if s.id.startswith("local_"):
                for key, prop in self._LOCAL_DEFENCE:
                    if key in s.id and prop not in self.base.properties:
                        return False   # e.g. local Armour craft on a pure Energy Shield base
        return True

    # ------------------------------------------------------------------ pools
    def pool(self, fossils: tuple[Fossil, ...] = (), blocked_tags: tuple[str, ...] = ()) -> list[PoolEntry]:
        divisor = self.rules["fossil_weight_divisor"]
        out = []
        for e in self.base_entries:
            tags = e.mod.tags
            if blocked_tags and any(t in tags for t in blocked_tags):
                continue
            w = e.weight
            for f in fossils:
                for tag, fw in f.weights:
                    if tag in tags:
                        w *= fw / divisor
            if w > 0:
                out.append(PoolEntry(e.mod, w, e.tier) if w != e.weight else e)
        return out

    # --------------------------------------------------------------- families
    def families(self) -> dict[str, Family]:
        fams: dict[str, Family] = {}

        def fam_for(m: Mod) -> Family:
            f = fams.get(m.signature)
            if f is None:
                f = fams[m.signature] = Family(m.signature, m.affix, generic_text(m.text),
                                               m.stats[0].id if m.stats else "")
            return f

        for e in sorted(self.base_entries, key=lambda e: e.tier):
            f = fam_for(e.mod)
            f.sources.add("roll")
            if e.mod.id in self.influenced:
                f.influence = True
                f.sources.add("influence")
            f.tiers.append(self._tier_json(e.mod, "roll", e.weight, f"T{e.tier}"))
        for ess, m in self.essence_options:
            if not m.stats:
                continue
            f = fam_for(m)
            f.sources.add("essence")
            f.tiers.append(self._tier_json(m, "essence", 0, ess.name, ess.id))
        for craft, m in self.bench_options:
            f = fam_for(m)
            f.sources.add("bench")
            f.tiers.append(self._tier_json(m, "bench", 0, f"Bench ({craft.master.split(',')[0]})"))
        for f in fams.values():
            f.tiers.sort(key=lambda t: (t["max"] is None, -(t["max"] or 0)))
        return fams

    @staticmethod
    def _tier_json(m: Mod, source: str, weight: float, label: str, label_id: str | None = None) -> dict:
        # min / max stay in stored units here: the planner works with them; the server converts for display
        s = m.stats[0] if m.stats else None
        out = {"mod_id": m.id, "source": source, "label": label, "text": m.text, "ilvl": m.required_level,
               "weight": weight, "min": s.min if s else None, "max": s.max if s else None}
        if label_id:            # the label is a game item (an essence): its id, for localized display
            out["label_id"] = label_id
        return out

    def make_targets(self, specs: list[dict]) -> list[Target]:
        fams = self.families()
        targets, seen = [], set()
        for spec in specs:
            sig = spec["signature"]
            fam = fams.get(sig)
            if fam is None:
                from ..messages import G, M
                raise ValueError(M("m.ctx.not_on_base", label=spec.get("label", sig),
                                   base=G("base", self.base.id, self.base.name), ilvl=self.ilvl,
                                   inf=str(list(self.influences)) if self.influences else M("m.ctx.none")))
            if sig in seen:
                from ..messages import M
                raise ValueError(M("m.ctx.duplicate", label=fam.label))
            seen.add(sig)
            mn = spec.get("min")
            targets.append(Target(len(targets), sig, fam.primary_stat,
                                  None if mn in (None, "") else float(mn), fam.label, fam.affix))
        for affix in ("prefix", "suffix"):
            n = sum(t.affix == affix for t in targets)
            if n > self.limits[affix]:
                from ..messages import M
                raise ValueError(M("m.ctx.too_many", n=n, side=affix, side_word=M(f"m.side.{affix}"),
                                   max=self.limits[affix]))
        return targets
