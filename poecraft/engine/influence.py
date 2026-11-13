"""Influence crafting: Shaper / Elder / Conqueror Exalted Orbs and Awakener's Orb.

What the game data says (base_items descriptions, checked in tests):
  * "<X>'s Exalted Orb - Adds <X> influence and a new <X> modifier to a rare item ... left click a high-level rare
    item with no influence": the item must have NO influence, and exactly one influenced modifier is added;
  * "Awakener's Orb - Destroys an item, applying its influence to another of the same item class. The second item
    is reforged as a rare item with both influence types and new modifiers";
  * "Orb of Dominance" (the former Maven's Orb) removes one influenced modifier and upgrades another; the odds are
    not in the data and Elevated modifiers have no spawn weights -> not supported.
The influence modifier pools themselves (tiers, values, spawn weights per influence and item class) come from the
same database as every other modifier: an influence adds a tag such as 'gloves_basilisk' to the item, and the mods
whose spawn weight is decided by that tag are that influence's pool (ItemContext.influenced).
"""
from __future__ import annotations

from .context import ItemContext, PoolEntry, Target, to_display

INFLUENCES = ("shaper", "elder", "crusader", "hunter", "redeemer", "warlord")


def influence_context(data, rules, base, ilvl: int, influences) -> ItemContext:
    return ItemContext(data, rules, base, ilvl, tuple(influences))


def exalt_pool(ctx_inf: ItemContext) -> list[PoolEntry]:
    """Mods an influence Exalt can add: those that roll only because of the influence tag."""
    return [e for e in ctx_inf.base_entries if e.mod.id in ctx_inf.influenced]


def exalt_chance(target: Target, pool: list[PoolEntry], blocked_groups=()) -> tuple[float, float]:
    """(chance per Exalt to add the target, weighted value chance among the hits).
    Both affix sides are assumed open - the most conservative case."""
    eligible = [e for e in pool if not any(g in blocked_groups for g in e.mod.groups)]
    total = sum(e.weight for e in eligible)
    hits = [e for e in eligible if target.satisfied_by(e.mod)]
    w = sum(e.weight for e in hits)
    if total <= 0 or w <= 0:
        return 0.0, 0.0
    q = sum(e.weight * target.value_chance(e.mod) for e in hits) / w
    return w / total, q


def target_for(ctx_inf: ItemContext, spec: dict, index: int) -> tuple[Target | None, str | None]:
    """Resolve an influence target against this base's pool for that influence, or explain why it can't be."""
    inf, sig, mn = spec["influence"], spec.get("signature"), spec.get("min")
    mn = None if mn in (None, "") else float(mn)
    fam = ctx_inf.families().get(sig)
    pool = exalt_pool(ctx_inf)
    if fam is None or not any(e.mod.signature == sig for e in pool):
        return None, None
    t = Target(index, sig, fam.primary_stat, mn, fam.label, fam.affix)
    if not any(t.satisfied_by(e.mod) for e in pool):
        top = max(e.mod.stats[0].max for e in pool if e.mod.signature == sig)
        from ..messages import M, N
        return None, M("m.inf.below", inf=N(inf.title()), top=to_display(fam.primary_stat, top))
    return t, None
