"""Veiled-modifier targets: the unveil pool of a base and the chance a target is among the unveil options.

Data: poecraft/data/veiled.py (unveiled outcomes with spawn weights). Assumptions (not in the export, marked
approximate wherever they are used): the options are distinct outcomes of the veiled mod's side, drawn by spawn
weight without replacement, never sharing a mod group with a mod already on the item.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..data.repository import Mod
from ..data.veiled import VeiledData
from .context import Target, generic_text


@dataclass
class VeiledFamily:
    signature: str
    affix: str
    label: str
    primary_stat: str
    mods: list = field(default_factory=list)       # UnveiledMod
    members: set = field(default_factory=set)      # Syndicate-member names whose own veiled mod unveils into it

    @property
    def generic(self) -> bool:
        return any(u.member is None for u in self.mods)


def all_families(data: VeiledData) -> dict[str, VeiledFamily]:
    out: dict[str, VeiledFamily] = {}
    for u in data.mods:
        m = u.mod
        f = out.get(m.signature)
        if f is None:
            f = out[m.signature] = VeiledFamily(m.signature, m.affix, generic_text(m.text), m.stats[0].id)
        f.mods.append(u)
        if u.member:
            f.members.add(u.member)
    return out


def placeholder(side: str) -> Mod:
    """The veiled modifier itself: occupies one slot on its side, has no stats until it is unveiled."""
    name = "VeiledPrefix" if side == "prefix" else "VeiledSuffix"
    return Mod(name, "Veiled", "veiled", side, name, 1, False, f"Veiled {side}", (name,), (), (), (), ())


def unveil_pool(data: VeiledData, ctx, side: str, blocked_groups=()) -> list[tuple[Mod, int]]:
    """Generic unveil outcomes of a veiled `side` modifier on this base and item level, by spawn weight."""
    blocked = set(blocked_groups)
    out = []
    for u in data.mods:
        m = u.mod
        if u.member or m.affix != side or m.required_level > ctx.ilvl or blocked & set(m.groups):
            continue
        w = m.spawn_weight(set(ctx.tags))
        if w > 0:
            out.append((m, w))
    return out


def chance_in_options(pool: list[tuple[Mod, int]], t: Target, k: int) -> tuple[float, float]:
    """(P(at least one of k options satisfies t), weight-averaged value chance of the satisfying outcomes).
    Exact for k distinct options drawn by weight without replacement."""
    hits = [(m, w) for m, w in pool if t.satisfied_by(m)]
    h = sum(w for _, w in hits)
    if h <= 0:
        return 0.0, 0.0
    q = sum(w * t.value_chance(m) for m, w in hits) / h
    misses = [w for m, w in pool if not t.satisfied_by(m)]

    def none_left(rest: list[int], total: float, picks: int) -> float:
        """P(the remaining `picks` draws from `rest` (+ the hit weight h) all miss)."""
        if picks == 0 or total <= 0:
            return 1.0
        return sum(w / total * none_left(rest[:i] + rest[i + 1:], total - w, picks - 1)
                   for i, w in enumerate(rest))

    return 1.0 - none_left(misses, float(sum(misses) + h), min(k, len(pool))), q
