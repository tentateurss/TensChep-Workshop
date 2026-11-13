"""Monte Carlo item simulator: applies currency to concrete items mod by mod.

Independent of analytic.py (no aggregation), used to verify the analytic numbers.
"""
from __future__ import annotations

import bisect
import random
from itertools import accumulate

from ..data.repository import Mod
from .context import PoolEntry, Target, tag_allowed


def sample_count(n_dist: dict[int, float], rng: random.Random) -> int:
    r = rng.random() * sum(n_dist.values())
    for n, p in sorted(n_dist.items()):
        r -= p
        if r <= 0:
            return n
    return max(n_dist)


class Simulator:
    def __init__(self, entries: list[PoolEntry], seed: int | None = None):
        self.rng = random.Random(seed)
        self._tables: dict = {}
        self._entries = [e for e in entries if e.weight > 0]

    def _table(self, restrict: str | None):
        if restrict not in self._tables:
            es = [e for e in self._entries if tag_allowed(restrict, e.mod.tags)]
            self._tables[restrict] = (es, list(accumulate(e.weight for e in es)))
        return self._tables[restrict]

    def pick(self, mods: list[Mod], limits: dict, restrict: str | None = None,
             only_affix: str | None = None) -> Mod | None:
        es, cum = self._table(restrict)
        if not es:
            return None
        groups = {g for m in mods for g in m.groups}
        count = {"prefix": 0, "suffix": 0}
        for m in mods:
            count[m.affix] += 1

        def ok(m: Mod) -> bool:
            return (count[m.affix] < limits[m.affix] and (only_affix is None or m.affix == only_affix)
                    and not any(g in groups for g in m.groups))

        total = cum[-1]
        for _ in range(48):    # rejection sampling is exact and fast while most of the pool is allowed
            e = es[bisect.bisect_right(cum, self.rng.random() * total)]
            if ok(e.mod):
                return e.mod
        allowed = [e for e in es if ok(e.mod)]
        if not allowed:
            return None
        return self.rng.choices([e.mod for e in allowed], weights=[e.weight for e in allowed])[0]

    def roll(self, n_dist: dict[int, float], limits: dict, existing: tuple[Mod, ...] = (),
             restrict_first: str | None = None) -> list[Mod]:
        """Chaos / Alchemy / Essence / Fossil / Alteration style reroll."""
        mods = list(existing)
        n = sample_count(n_dist, self.rng)
        first = True
        while len(mods) < n:
            m = self.pick(mods, limits, restrict_first if first else None)
            first = False
            if m is None:
                break
            mods.append(m)
        return mods

    def annul(self, mods: list[Mod], fixed: tuple[Mod, ...] = (), locked: str | None = None,
              extra: int = 0) -> tuple[list[Mod], str]:
        """Orb of Annulment on concrete mods (uniform over the unlocked side).
        extra = removable mods not in the list (the metacraft). Returns (mods, what was hit):
        'mod', 'fixed' (a fixed mod, e.g. the essence mod), 'extra' or 'none'.
        Fractured mods must not be passed in `fixed` - Annulment cannot pick them."""
        cands = [i for i, m in enumerate(mods) if m.affix != locked]
        n_fixed = sum(m.affix != locked for m in fixed)
        n = len(cands) + n_fixed + extra
        if n == 0:
            return mods, "none"
        r = self.rng.randrange(n)
        if r < len(cands):
            out = list(mods)
            del out[cands[r]]
            return out, "mod"
        return mods, ("fixed" if r < len(cands) + n_fixed else "extra")


def scour_side(mods: list[Mod], keep: str) -> list[Mod]:
    """Orb of Scouring under '<keep>es Cannot Be Changed'."""
    return [m for m in mods if m.affix == keep]


def satisfies(mods: list[Mod], required: list[Target], bench: Target | None, limits: dict,
              bench_groups: tuple[str, ...] = ()) -> bool:
    if not all(any(t.satisfied_by(m) for m in mods) for t in required):
        return False
    if bench is None or any(bench.satisfied_by(m) for m in mods):
        return True
    return bench_open(mods, bench, limits, bench_groups)


def satisfies_multi(mods: list[Mod], required: list[Target], benches: list, limits: dict) -> bool:
    """benches = [(Target, groups)]; 2+ missing crafts need Multimod (one more suffix slot)."""
    if not all(any(t.satisfied_by(m) for m in mods) for t in required):
        return False
    pending = [(t, g) for t, g in benches if not any(t.satisfied_by(m) for m in mods)]
    present = {grp for m in mods for grp in m.groups}
    if any(grp in present for _, g in pending for grp in g):
        return False
    np_ = sum(m.affix == "prefix" for m in mods) + sum(t.affix == "prefix" for t, _ in pending)
    ns_ = sum(m.affix == "suffix" for m in mods) + sum(t.affix == "suffix" for t, _ in pending) + \
        (1 if len(pending) >= 2 else 0)
    return np_ <= limits["prefix"] and ns_ <= limits["suffix"]


def bench_open(mods: list[Mod], bench: Target, limits: dict, bench_groups: tuple[str, ...] = ()) -> bool:
    count = sum(m.affix == bench.affix for m in mods)
    if count >= limits[bench.affix]:
        return False
    return not any(g in bench_groups for m in mods for g in m.groups)


def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 1.0
    p = hits / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return max(0.0, c - h), min(1.0, c + h)
