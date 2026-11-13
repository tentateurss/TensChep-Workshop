"""Eldritch implicit planning: which Ember / Ichor to use for a target implicit, and its odds.

Mechanics modelled (from the RePoE export, see data/eldritch.py):
  * an Ember adds a Searing Exarch implicit, an Ichor an Eater of Worlds implicit, at the tier named by the
    currency; each application replaces only that side's Eldritch implicit, never explicits or the other side;
  * which implicit appears is drawn by spawn weight among that tier's implicits for the item's tags;
  * Eldritch implicits do not use explicit affix slots, and explicit currency (Chaos, Essence, Fossil, Harvest,
    Alteration, Exalted, Annulment, Scouring) never changes them - so the Eldritch part of a plan is independent
    of the explicit part, and its cost simply adds.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache

from ..data.eldritch import SIDE_NAMES, EldritchCurrency, EldritchData, EldritchMod
from ..messages import CUR, J, M, N
from .context import generic_text, to_display


@dataclass
class EldritchFamily:
    key: str
    side: str
    label: str
    primary_stat: str
    tiers: dict = field(default_factory=dict)      # tier -> EldritchMod (one per tier for this stat line)


@dataclass
class EldritchOption:
    label: str
    side: str
    currency: EldritchCurrency
    tier: int
    mod: EldritchMod
    p: float              # chance per application to get the target implicit (incl. value roll)
    value_chance: float
    price: float

    @property
    def cost(self) -> float:
        return self.price / self.p

    @property
    def variance(self) -> float:
        return self.price ** 2 * (1 - self.p) / self.p ** 2


@lru_cache(maxsize=4)
def all_families(data: EldritchData) -> dict[str, EldritchFamily]:
    fams: dict[str, EldritchFamily] = {}
    for m in data.mods:
        f = fams.get(m.key)
        if f is None:
            f = fams[m.key] = EldritchFamily(m.key, m.side, generic_text(m.text), m.stats[0].id)
        if m.tier not in f.tiers or m.stats[0].max > f.tiers[m.tier].stats[0].max:
            f.tiers[m.tier] = m
    return fams


def base_families(data: EldritchData, item_class: str, tags) -> dict[str, EldritchFamily]:
    """Families that can roll on this base (class allowed by an Ember/Ichor and spawn weight > 0)."""
    out = {}
    for key, f in all_families(data).items():
        if item_class not in data.classes(f.side):
            continue
        tiers = {t: m for t, m in f.tiers.items() if m.weight(tags) > 0}
        if tiers:
            out[key] = EldritchFamily(f.key, f.side, f.label, f.primary_stat, tiers)
    return out


def _value_chance(mod: EldritchMod, stat_id: str, min_value: float | None) -> float:
    s = next((x for x in mod.stats if x.id == stat_id), None)
    if s is None or min_value is None:
        return 1.0
    if s.max < min_value:
        return 0.0
    if min_value <= s.min:
        return 1.0
    if float(s.min).is_integer() and float(s.max).is_integer():
        return (s.max - math.ceil(min_value) + 1) / (s.max - s.min + 1)
    return (s.max - min_value) / (s.max - s.min)


def options(data: EldritchData, rules: dict, ctx, spec: dict, prices) -> tuple[list[EldritchOption], str | None]:
    """Every Ember/Ichor that can produce the target implicit on this base, or (None, reason) if none can."""
    side, key, mn = spec["eldritch"], spec.get("signature"), spec.get("min")
    mn = None if mn in (None, "") else float(mn)
    fams_all = all_families(data)
    fam = fams_all.get(key)
    base, cls = ctx.base, ctx.base.item_class
    name = SIDE_NAMES.get(side, side)
    if fam is None:
        other = {"exarch": "eater", "eater": "exarch"}.get(side, "")
        swapped = other + key[key.find("|"):] if "|" in (key or "") else None
        if swapped in fams_all:
            return [], M("m.eld.wrong_side", other=N(SIDE_NAMES[other]), side=N(name),
                         cur=M("m.eld.ichor" if other == "eater" else "m.eld.ember"))
        return [], M("m.eld.no_family")
    if cls not in data.classes(side):
        tagged = sum(1 for m in data.mods if m.side == side and m.weight(ctx.tags) > 0)
        if tagged:      # e.g. amulets: the export lists such implicits, but no currency in it adds them
            return [], M("m.eld.class_listed", cls=cls.lower(), cls_name=N(cls), n=tagged, side=N(name),
                         classes=J([N(c) for c in data.classes(side)], ", "))
        return [], M("m.eld.class_none", cls=N(cls), classes=J([N(c) for c in data.classes(side)], ", "))
    if ctx.influences:
        return [], M("m.eld.influenced")
    tiers = {t: m for t, m in fam.tiers.items() if m.weight(ctx.tags) > 0}
    if not tiers:
        where = sorted({c for c in data.classes(side)})
        return [], M("m.eld.not_on_class", side=N(name), cls=N(cls), classes=J([N(c) for c in where], ", "))
    lvl = min(m.required_level for m in tiers.values())
    if ctx.ilvl < lvl:
        return [], M("m.eld.ilvl", lvl=lvl, ilvl=ctx.ilvl)
    tier_of = rules["eldritch_tier_names"]
    out = []
    for cur in data.currencies:
        if cur.side != side or cls not in cur.item_classes:
            continue
        tier = tier_of.get(cur.tier_name)
        mod = tiers.get(tier)
        if mod is None:
            continue
        vc = _value_chance(mod, fam.primary_stat, mn)
        if vc <= 0:
            continue
        pool = [m for m in data.mods if m.side == side and m.tier == tier and m.weight(ctx.tags) > 0]
        total = sum(m.weight(ctx.tags) for m in pool)
        w = sum(m.weight(ctx.tags) for m in pool if m.key == key)
        if total <= 0 or w <= 0:
            continue
        out.append(EldritchOption(fam.label, side, cur, tier, mod, w / total * vc, vc, prices.get(cur.name)))
    if not out:
        better = [t for t, m in tiers.items() if _value_chance(m, fam.primary_stat, mn) > 0]
        if better:      # tier 1 is the strongest, so the weakest acceptable tier is the largest number
            from .planner import rule_note
            return [], M("m.eld.needs_tier", tier=max(better), note=rule_note("eldritch_conflict_note"))
        top = max(m.stats[0].max for m in tiers.values())
        return [], M("m.eld.below", top=to_display(fam.primary_stat, top))      # as the game shows it
    return out, None
