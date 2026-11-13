"""Fast semi-analytic probability engine for weighted mod rolling.

PoE picks each new mod by weight from every mod that is still allowed (affix slot free,
mod group not already on the item). Tracking every mod exactly is combinatorially
infeasible, so we split groups into:

* relevant groups - any group containing a target stat line (or blocking the bench
  finish, or present on an item being recovered). These are tracked exactly with a bitmask.
* other groups   - aggregated per affix. After k of them were picked, the remaining
  weight is estimated with the size-biased mean group weight (sum w^2 / sum w).

That aggregation is the only approximation; results are cross-checked by the Monte Carlo
simulator in simulate.py and both numbers are shown to the user.

On top of the same item states, RecoveryMDP computes the optimal recovery policy
(Exalted Orb, metacraft-filtered Exalt, Orb of Annulment, Prefixes/Suffixes Cannot Be
Changed + Annul/Scour/Chaos, or restart) by value iteration.
"""
from __future__ import annotations

import time

from collections import defaultdict
from dataclasses import dataclass, field

from ..data.repository import Mod
from .context import PoolEntry, Target, tag_allowed

State = tuple[int, int, int, int]   # (removed relevant groups mask, hit targets mask, k_other_prefix, k_other_suffix)
RESTART: State = (-1, -1, -1, -1)   # outcome that destroys something the state cannot represent -> start over


@dataclass
class _Group:
    affix: str
    mods: list = field(default_factory=list)   # (weight, target index or -1, tags)
    _cache: dict = field(default_factory=dict)

    def weights(self, restrict: str | None):
        if restrict not in self._cache:
            total, hits = 0.0, defaultdict(float)
            for w, t, tags in self.mods:
                if not tag_allowed(restrict, tags):
                    continue
                total += w
                if t >= 0:
                    hits[t] += w
            self._cache[restrict] = (total, dict(hits))
        return self._cache[restrict]


@dataclass
class _Other:
    groups: dict = field(default_factory=lambda: defaultdict(list))  # key -> [(w, tags)]
    _cache: dict = field(default_factory=dict)

    def stats(self, restrict: str | None):
        if restrict not in self._cache:
            totals = [sum(w for w, _ in g) for g in self.groups.values()]
            total = sum(totals)
            mean = sum(t * t for t in totals) / total if total else 0.0
            rtotal = total if restrict is None else sum(
                w for g in self.groups.values() for w, tags in g if tag_allowed(restrict, tags))
            self._cache[restrict] = (total, mean, len(totals), rtotal)
        return self._cache[restrict]

    def remaining(self, k: int, restrict: str | None) -> float:
        total, mean, n, rtotal = self.stats(restrict)
        if k >= n or total <= 0:
            return 0.0
        return rtotal * max(0.0, (total - k * mean) / total)


def _key(m: Mod) -> tuple[str, str]:
    return (m.group, m.affix)


def _merge(outs) -> list[tuple[float, State]]:
    acc: dict = defaultdict(float)
    for p, s in outs:
        if p > 0:
            acc[s] += p
    return [(p, s) for s, p in acc.items()]


class GroupedPool:
    def __init__(self, entries: list[PoolEntry], targets: list[Target],
                 existing: tuple[Mod, ...] = (), block_groups: tuple[str, ...] = (),
                 track: tuple[Mod, ...] = (), layout: "GroupedPool | None" = None,
                 protected: tuple[Mod, ...] = ()):
        """existing: fixed mods (e.g. the essence mod) that no operation removes.
        track: mods of a concrete item whose groups must be tracked exactly (recovery).
        layout: reuse another pool's relevant-group indexing so states are interchangeable
        (fossil rolls feeding recovery steps that use the normal pool).
        protected: the subset of `existing` that is fractured - Annulment cannot pick it and a locked Scour keeps
        it (other fixed mods removed by those actions still mean RESTART)."""
        self.targets = targets
        self.np_prot = sum(m.affix == "prefix" for m in protected)
        self.ns_prot = sum(m.affix == "suffix" for m in protected)
        existing_keys = {_key(m) for m in existing}
        existing_groups = {g for m in existing for g in m.groups}
        self.np0 = sum(m.affix == "prefix" for m in existing)
        self.ns0 = sum(m.affix == "suffix" for m in existing)
        self.hits0 = 0
        for m in existing:
            for t in targets:
                if t.satisfied_by(m):
                    self.hits0 |= 1 << t.index
        sigs = {t.signature for t in targets}
        relevant_keys = {_key(e.mod) for e in entries if e.mod.signature in sigs or e.mod.group in block_groups}
        relevant_keys |= {_key(m) for m in track}
        self.relevant: list[_Group] = []
        index: dict = {}
        if layout is not None:
            for k, i in sorted(layout.index.items(), key=lambda x: x[1]):
                index[k] = i
                self.relevant.append(_Group(k[1]))
            relevant_keys |= set(index)
        self.other = {"prefix": _Other(), "suffix": _Other()}
        for e in entries:
            k = _key(e.mod)
            if k in existing_keys or any(g in existing_groups for g in e.mod.groups):
                continue
            t_idx = next((t.index for t in targets if t.satisfied_by(e.mod)), -1)
            if k in relevant_keys:
                if k not in index:
                    index[k] = len(self.relevant)
                    self.relevant.append(_Group(e.mod.affix))
                self.relevant[index[k]].mods.append((e.weight, t_idx, e.mod.tags))
            else:
                self.other[e.mod.affix].groups[k].append((e.weight, e.mod.tags))
        for m in track:   # crafted / essence-only mods on the item: tracked, but they cannot roll (weight 0)
            k = _key(m)
            if k not in index and k not in existing_keys:
                index[k] = len(self.relevant)
                t_idx = next((t.index for t in targets if t.satisfied_by(m)), -1)
                self.relevant.append(_Group(m.affix, [(0.0, t_idx, m.tags)]))
        self.index = index
        self.block_mask = self.mask_for_groups(block_groups)
        self.prefix_mask = sum(1 << i for i, g in enumerate(self.relevant) if g.affix == "prefix")
        self.target_groups = {t.index: [i for i, g in enumerate(self.relevant) if any(tt == t.index for _, tt, _ in g.mods)]
                              for t in targets}

    def mask_for_groups(self, groups) -> int:
        return sum(1 << i for k, i in self.index.items() if k[0] in groups)

    def initial(self) -> State:
        return (0, self.hits0, 0, 0)

    def counts(self, s: State) -> tuple[int, int]:
        removed, _, kop, kos = s
        np_ = self.np0 + kop + bin(removed & self.prefix_mask).count("1")
        ns_ = self.ns0 + kos + bin(removed & ~self.prefix_mask).count("1")
        return np_, ns_

    def state_of(self, mods) -> State:
        """Concrete (non-fixed) mods -> state."""
        removed, hits, kop, kos = 0, self.hits0, 0, 0
        for m in mods:
            i = self.index.get(_key(m))
            if i is not None:
                removed |= 1 << i
            elif m.affix == "prefix":
                kop += 1
            else:
                kos += 1
            for t in self.targets:
                if t.satisfied_by(m):
                    hits |= 1 << t.index
        return (removed, hits, kop, kos)

    def options(self, s: State, limits: dict, restrict: str | None = None, only_affix: str | None = None):
        """All transitions from state s for one new mod: (total weight, [(weight, next_state)])."""
        removed, hits, kop, kos = s
        np_, ns_ = self.counts(s)
        open_ = {"prefix": np_ < limits["prefix"], "suffix": ns_ < limits["suffix"]}
        if only_affix:
            open_ = {a: v and a == only_affix for a, v in open_.items()}
        opts, total = [], 0.0
        for i, g in enumerate(self.relevant):
            if removed >> i & 1 or not open_[g.affix]:
                continue
            W, hw = g.weights(restrict)
            if W <= 0:
                continue
            total += W
            nb = removed | (1 << i)
            acc = 0.0
            for t, w in hw.items():
                if w > 0:
                    opts.append((w, (nb, hits | (1 << t), kop, kos)))
                    acc += w
            if W - acc > 1e-9:
                opts.append((W - acc, (nb, hits, kop, kos)))
        if open_["prefix"]:
            w = self.other["prefix"].remaining(kop, restrict)
            if w > 0:
                total += w
                opts.append((w, (removed, hits, kop + 1, kos)))
        if open_["suffix"]:
            w = self.other["suffix"].remaining(kos, restrict)
            if w > 0:
                total += w
                opts.append((w, (removed, hits, kop, kos + 1)))
        return total, opts

    # --------------------------------------------------------- mod removal
    def _rehit(self, removed: int, hits: int) -> int:
        """Drop target hits whose providing group is no longer on the item."""
        for t in self.targets:
            bit = 1 << t.index
            if hits & bit and not self.hits0 & bit and not any(removed >> j & 1 for j in self.target_groups[t.index]):
                hits &= ~bit
        return hits

    def annul(self, s: State, locked: str | None = None, extra: int = 0):
        """Orb of Annulment: removes one mod uniformly at random from the unlocked side.
        extra = additional removable mods outside the state (the metacraft itself).
        Returns (number of candidates, [(p, next_state)]). Removing a fixed mod -> RESTART.
        (In PoE 1 removing a mod never invalidates the others: influence is not a mod and
        group limits only get looser, so no re-roll of an 'invalid' item is needed.)"""
        removed, hits, kop, kos = s
        cands = []
        for i, g in enumerate(self.relevant):
            if removed >> i & 1 and g.affix != locked:
                nb = removed & ~(1 << i)
                cands.append((nb, self._rehit(nb, hits), kop, kos))
        if locked != "prefix":
            cands += [(removed, hits, kop - 1, kos)] * kop
        if locked != "suffix":
            cands += [(removed, hits, kop, kos - 1)] * kos
        fixed = ((self.np0 - self.np_prot) if locked != "prefix" else 0) + \
            ((self.ns0 - self.ns_prot) if locked != "suffix" else 0)          # fractured mods are never picked
        n = len(cands) + fixed + extra
        if n == 0:
            return 0, []
        outs = [(1 / n, c) for c in cands]
        if fixed:
            outs.append((fixed / n, RESTART))
        if extra:
            outs.append((extra / n, s))
        return n, _merge(outs)

    def scour_side(self, s: State, keep: str) -> State:
        """Orb of Scouring under '<keep>es Cannot Be Changed': every mod of the other side goes."""
        removed, hits, kop, kos = s
        if ((self.ns0 - self.ns_prot) if keep == "prefix" else (self.np0 - self.np_prot)):
            return RESTART                    # a non-fractured fixed mod would be scoured away
        if keep == "prefix":
            nb, kop2, kos2 = removed & self.prefix_mask, kop, 0
        else:
            nb, kop2, kos2 = removed & ~self.prefix_mask, 0, kos
        return (nb, self._rehit(nb, hits), kop2, kos2)

    # ------------------------------------------------------------- processes
    def roll(self, n_dist: dict[int, float], limits: dict, restrict_first: str | None = None,
             start: dict[State, float] | None = None) -> dict[State, float]:
        """Distribution of final states after adding mods until the item has n mods (n ~ n_dist)."""
        states = dict(start) if start else {self.initial(): 1.0}
        max_n = max(n_dist)
        final: dict[State, float] = defaultdict(float)
        first = True
        while states:
            nxt: dict[State, float] = defaultdict(float)
            for s, p in states.items():
                c = sum(self.counts(s))
                tail = sum(q for n, q in n_dist.items() if n >= c)
                p_stop = 1.0 if c >= max_n or tail <= 0 else n_dist.get(c, 0.0) / tail
                if p_stop > 0:
                    final[s] += p * p_stop
                cont = p * (1 - p_stop)
                if cont <= 0:
                    continue
                total, opts = self.options(s, limits, restrict_first if first else None)
                if total <= 0:
                    final[s] += cont
                    continue
                for w, ns in opts:
                    nxt[ns] += cont * w / total
            states, first = nxt, False
        return final

    def slam_chain(self, start: dict[State, float], limits: dict, steps: int, needed_mask: int):
        """Regal/Exalt sequence where every slam must add a still-missing target (else: abort).
        With nothing left to hit, a slam only has to avoid blocking the bench craft.
        Returns (final states that survived every slam, probability of reaching each slam)."""
        states = dict(start)
        reach = []
        for _ in range(steps):
            reach.append(sum(states.values()))
            nxt: dict[State, float] = defaultdict(float)
            for s, p in states.items():
                total, opts = self.options(s, limits)
                if total <= 0:
                    continue
                missing = needed_mask & ~s[1]
                for w, ns in opts:
                    ok = bool(ns[1] & missing) if missing else not (ns[0] & self.block_mask)
                    if ok:
                        nxt[ns] += p * w / total
            states = nxt
        return states, reach

    # ------------------------------------------------------------- predicates
    def success(self, states: dict[State, float], required_mask: int, bench: Target | None, limits: dict) -> float:
        return sum(p for s, p in states.items() if self.is_success(s, required_mask, bench, limits))

    def is_success(self, s: State, required_mask: int, bench: Target | None, limits: dict) -> bool:
        if s == RESTART:
            return False
        removed, hits, _, _ = s
        if hits & required_mask != required_mask:
            return False
        if bench is None or hits >> bench.index & 1:
            return True
        if removed & self.block_mask:
            return False
        np_, ns_ = self.counts(s)
        return (np_ if bench.affix == "prefix" else ns_) < limits[bench.affix]

    def multi_success(self, s: State, required_mask: int, benches: list, limits: dict):
        """Bench finish with several crafts. benches = [(Target, block mask)].
        Two or more missing crafts need Multimod, which itself takes a suffix slot.
        Returns (ok, list of targets still to craft)."""
        removed, hits, _, _ = s
        if hits & required_mask != required_mask:
            return False, []
        pending = [t for t, m in benches if not hits >> t.index & 1]
        if any(removed & m for t, m in benches if not hits >> t.index & 1):
            return False, []
        np_, ns_ = self.counts(s)
        need_p = sum(t.affix == "prefix" for t in pending)
        need_s = sum(t.affix == "suffix" for t in pending) + (1 if len(pending) >= 2 else 0)
        return np_ + need_p <= limits["prefix"] and ns_ + need_s <= limits["suffix"], pending


# =============================================================== recovery MDP
class MdpTooLarge(Exception):
    """The recovery problem is too big to solve here: too many item states, or (timed_out) its time budget ran out.
    Either way the optional recovery variant is skipped; nothing about the solved cases changes."""
    def __init__(self, msg: str = "", timed_out: bool = False):
        super().__init__(msg)
        self.timed_out = timed_out


@dataclass(frozen=True)
class MdpAction:
    key: str
    label: str
    cost: float
    status: str = "computed"
    apps: float = 1.0      # currency/bench applications this action takes (for the min_attempts objective)


class RecoveryMDP:
    """Optimal crafting policy over GroupedPool states of a rare item.

    Actions (when legal): Exalted Orb; bench 'Cannot roll Attack/Caster Modifiers' + Exalt +
    remove crafted mods; Orb of Annulment; '<side>es Cannot Be Changed' + Annulment (the
    metacraft itself can be the annulled mod) / + Scouring / + Chaos Orb; restart.
    Done states cost `terminal(s)`. Restart costs `restart_cost + V_start`.
    The metacraft occupies a slot of its own side, so it needs a free one, and it is consumed by
    the Scour/Chaos it protects against (it sits on the side being changed)."""

    def __init__(self, gp: GroupedPool, limits: dict, rare_n: dict, costs: dict, restart_cost: float,
                 done, terminal=None, allow_chaos_lock: bool = True, max_states: int = 25000):
        self.gp, self.limits, self.rare_n, self.costs = gp, limits, rare_n, costs
        self.restart_cost = restart_cost
        self.done = done
        self.terminal = terminal or (lambda s: 0.0)
        self.allow_chaos_lock = allow_chaos_lock
        self.max_states = max_states
        self.vstart = None
        self._roll_cache: dict = {}
        self.deadline: float | None = None     # time.monotonic() value; None = no time budget (the default)

    def _tick(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise MdpTooLarge("time limit reached", timed_out=True)

    def _actions(self, s: State):
        gp, L, c = self.gp, self.limits, self.costs
        np_, ns_ = gp.counts(s)
        out = []

        def add(key, label, cost, outs, status="computed", apps=1.0):
            outs = _merge(outs)
            if outs and not (len(outs) == 1 and outs[0][1] == s):
                out.append((MdpAction(key, label, cost, status, apps), outs))

        if np_ < L["prefix"] or ns_ < L["suffix"]:
            total, opts = gp.options(s, L)
            if total > 0:
                add("slam", "Exalted Orb", c["exalt"], [(w / total, ns) for w, ns in opts])
        if ns_ < L["suffix"]:
            lim = {"prefix": L["prefix"], "suffix": L["suffix"] - 1}   # the metacraft holds a suffix slot
            for tag in ("attack", "caster"):
                if f"no_{tag}" in c:
                    total, opts = gp.options(s, lim, restrict="!" + tag)
                    if total > 0:
                        add(f"slam_no_{tag}", f"Bench 'Cannot roll {tag.title()} Modifiers' → Exalted Orb → "
                            f"remove crafted mods", c[f"no_{tag}"] + c["exalt"] + c["remove_crafted"],
                            [(w / total, ns) for w, ns in opts], apps=3.0)
        n, outs = gp.annul(s)
        if n:
            add("annul", "Orb of Annulment", c["annul"], outs)
        for keep, meta, free in (("prefix", "meta_p", ns_ < L["suffix"]), ("suffix", "meta_s", np_ < L["prefix"])):
            if meta not in c or not free:
                continue
            other = "suffix" if keep == "prefix" else "prefix"
            word = keep.title() + "es"
            n, outs = gp.annul(s, locked=keep, extra=1)
            if n > 1:
                add(f"lock_annul_{keep}", f"Bench '{word} Cannot Be Changed' → Orb of Annulment "
                    f"(only {other}es can go) → remove crafted mods",
                    c[meta] + c["annul"] + (1 - 1 / n) * c["remove_crafted"], outs, apps=2.0 + (1 - 1 / n))
            s2 = gp.scour_side(s, keep)
            if s2 != s:
                add(f"lock_scour_{keep}", f"Bench '{word} Cannot Be Changed' → Orb of Scouring "
                    f"(wipes all {other}es)", c[meta] + c["scour"], [(1.0, s2)], apps=2.0)
                if self.allow_chaos_lock and s2 != RESTART:
                    final = self._roll_cache.get(s2)
                    if final is None:
                        final = self._roll_cache[s2] = gp.roll(self.rare_n, L, start={s2: 1.0})
                    add(f"lock_chaos_{keep}", f"Bench '{word} Cannot Be Changed' → Chaos Orb (rerolls {other}es)",
                        c[meta] + c["chaos"], [(p, st) for st, p in final.items()], "approximate", apps=2.0)
        return out

    def build(self, starts) -> None:
        trans: dict = {}
        stack = [s for s in starts if s != RESTART]
        while stack:
            s = stack.pop()
            if s in trans:
                continue
            if self.done(s):
                trans[s] = None
                continue
            acts = self._actions(s)
            trans[s] = acts
            if not len(trans) & 63:
                self._tick()
            if len(trans) > self.max_states:
                raise MdpTooLarge(f"more than {self.max_states} item states")
            for _, outs in acts:
                for _, s2 in outs:
                    if s2 != RESTART and s2 not in trans:
                        stack.append(s2)
        # fullest items first: Gauss-Seidel then propagates values back from finished items quickly
        self.states = sorted(trans, key=lambda s: -sum(self.gp.counts(s)))
        self.idx = {s: i for i, s in enumerate(self.states)}
        self.term = [self.terminal(s) if trans[s] is None else None for s in self.states]
        self.acts = [None if trans[s] is None else
                     [(a, [(p, -1 if s2 == RESTART else self.idx[s2]) for p, s2 in outs]) for a, outs in trans[s]]
                     for s in self.states]
        self.policy = [None] * len(self.states)   # index into acts[i]; None = restart / done
        self.V = [t if t is not None else 0.0 for t in self.term]

    # ---------------------------------------------------------- solving
    def _vi(self, vstart: float) -> None:
        rs = self.restart_cost + vstart
        V = self.V
        for i, acts in enumerate(self.acts):
            if acts is not None:
                V[i] = min(V[i], rs) if V[i] > 0 else rs
        for _ in range(3000):
            self._tick()
            delta = 0.0
            for i, acts in enumerate(self.acts):
                if acts is None:
                    continue
                best, ba = rs, None
                for k, (a, outs) in enumerate(acts):
                    v = a.cost
                    for p, j in outs:
                        v += p * (rs if j < 0 else V[j])
                    if v < best - 1e-12:
                        best, ba = v, k
                d = abs(best - V[i])
                if d > delta:
                    delta = d
                V[i] = best
                self.policy[i] = ba
            if delta <= 1e-9 * max(1.0, rs):
                break

    def _evaluate(self) -> None:
        """Fixed policy: V(s) = A(s) + R(s) * V_start, R = probability of eventually restarting."""
        A = [t if t is not None else 0.0 for t in self.term]
        R = [0.0] * len(self.states)
        for _ in range(20000):
            self._tick()
            delta = 0.0
            for i, acts in enumerate(self.acts):
                if acts is None:
                    continue
                k = self.policy[i]
                if k is None:
                    na, nr = self.restart_cost, 1.0
                else:
                    a, outs = acts[k]
                    na, nr = a.cost, 0.0
                    for p, j in outs:
                        if j < 0:
                            na += p * self.restart_cost
                            nr += p
                        else:
                            na += p * A[j]
                            nr += p * R[j]
                d = max(abs(na - A[i]), abs(nr - R[i]) * max(1.0, na))
                if d > delta:
                    delta = d
                A[i], R[i] = na, nr
            if delta <= 1e-10 * max(1.0, max(A)):
                break
        self.A, self.R = A, R

    def solve(self, d0: dict[State, float], c_attempt: float, v_upper: float) -> float:
        """Strategy mode: V_start = c_attempt + E_d0[V(s)], restart -> V_start (fixed point)."""
        self.build(list(d0))
        starts = [(p, self.idx[s]) for s, p in d0.items() if s in self.idx]
        p_lost = sum(p for s, p in d0.items() if s not in self.idx)   # RESTART outcomes of the roll itself
        vstart = v_upper
        for _ in range(60):
            self._vi(vstart)
            self._evaluate()
            a0 = c_attempt + sum(p * self.A[i] for p, i in starts) + p_lost * self.restart_cost
            r0 = sum(p * self.R[i] for p, i in starts) + p_lost
            if r0 >= 1 - 1e-12:
                vstart = float("inf")
                break
            new = a0 / (1 - r0)
            converged = abs(new - vstart) <= 1e-7 * max(1.0, vstart)
            vstart = new
            if converged:
                break
        self.vstart = vstart
        self.p_attempt = 1 - r0 if vstart < float("inf") else 0.0
        self.d0 = starts
        return vstart

    def _starts(self):
        return self.d0, 1 - sum(p for p, _ in self.d0)   # (index starts, mass lost to RESTART in the roll itself)

    def per_item(self, metric, attempt_value: float, restart_value: float) -> float:
        """Expected total of an arbitrary per-action metric per finished item, under the solved policy
        (same restart probabilities R as the cost). Used for the min_attempts objective."""
        M = [0.0] * len(self.states)
        for _ in range(20000):
            delta = 0.0
            for i, acts in enumerate(self.acts):
                if acts is None:
                    continue
                k = self.policy[i]
                if k is None:
                    m = restart_value
                else:
                    a, outs = acts[k]
                    m = metric(a) + sum(p * (restart_value if j < 0 else M[j]) for p, j in outs)
                if abs(m - M[i]) > delta:
                    delta = abs(m - M[i])
                M[i] = m
            if delta <= 1e-10 * max(1.0, max(M)):
                break
        starts, p_lost = self._starts()
        a0 = attempt_value + sum(p * M[i] for p, i in starts) + p_lost * restart_value
        r0 = sum(p * self.R[i] for p, i in starts) + p_lost
        return a0 / (1 - r0) if r0 < 1 else float("inf")

    def cost_moments(self, c_attempt: float) -> tuple[float, float]:
        """(E[C], E[C^2]) of the total cost per finished item under the solved policy (strategy mode).
        Second moments satisfy W(s) = c^2 + 2c E[Y] + E[Y^2] for C = c + Y; like the first moment they split as
        W(s) = B(s) + R(s) * W_start, so W_start is solved exactly instead of by slow iteration."""
        vs, rc = self.vstart, self.restart_cost
        V = [self.A[i] + self.R[i] * vs for i in range(len(self.states))]
        B = [t * t if t is not None else 0.0 for t in self.term]
        restart_v, restart_b = rc + vs, rc * rc + 2 * rc * vs
        for _ in range(20000):
            delta = 0.0
            for i, acts in enumerate(self.acts):
                if acts is None:
                    continue
                k = self.policy[i]
                if k is None:
                    b = restart_b
                else:
                    a, outs = acts[k]
                    ey = sum(p * (restart_v if j < 0 else V[j]) for p, j in outs)
                    b = a.cost * a.cost + 2 * a.cost * ey + sum(p * (restart_b if j < 0 else B[j]) for p, j in outs)
                if abs(b - B[i]) > delta:
                    delta = abs(b - B[i])
                B[i] = b
            if delta <= 1e-10 * max(1.0, max(B)):
                break
        starts, p_lost = self._starts()
        r0 = sum(p * self.R[i] for p, i in starts) + p_lost
        num = sum(p * (c_attempt ** 2 + 2 * c_attempt * V[i] + B[i]) for p, i in starts) + \
            p_lost * (c_attempt ** 2 + 2 * c_attempt * restart_v + restart_b)
        return vs, num / (1 - r0) if r0 < 1 else float("inf")

    def solve_constant(self, starts, vstart: float) -> None:
        """Recovery mode: restarting means paying for a fresh plan whose cost is known."""
        self.build(starts)
        self.vstart = vstart
        self._vi(vstart)
        self._evaluate()

    def value(self, s: State) -> float:
        if s == RESTART:
            return self.restart_cost + self.vstart
        return self.V[self.idx[s]]

    def action_at(self, s: State) -> MdpAction | None:
        i = self.idx.get(s)
        if i is None or self.acts[i] is None or self.policy[i] is None:
            return None
        return self.acts[i][self.policy[i]][0]

    def no_restart_values(self) -> list[float]:
        """Optimal expected cost to finish when starting over is forbidden (inf if impossible).
        Answers 'what does it cost to finish THIS item' (e.g. refilling suffixes after a locked Scour)."""
        if getattr(self, "_vnr", None) is not None:
            return self._vnr
        penalty = 1e7     # restarting costs this much: any finishable state ends far below it
        # warm start from the solved values: allowing restarts can only be cheaper, so they are a lower bound
        V = list(self.V)
        # Exalt<->Annul loops make this chain mix slowly (contraction ~0.999 per sweep), so every few
        # sweeps the dominant geometric mode is extrapolated away (Aitken / extrapolated value iteration);
        # the sweeps that follow correct any overshoot, and convergence is still checked on plain sweeps.
        prev_delta, since_jump = None, 0
        for _ in range(20000):
            old = list(V)
            delta = 0.0
            for i, acts in enumerate(self.acts):
                if acts is None:
                    continue
                best = penalty
                for a, outs in acts:
                    v = a.cost
                    for p, j in outs:
                        v += p * (penalty if j < 0 else V[j])
                    if v < best:
                        best = v
                d = abs(best - V[i]) / max(1.0, best)
                if d > delta:
                    delta = d
                V[i] = best
            if delta <= 1e-7:
                break
            since_jump += 1
            if prev_delta and since_jump >= 5:
                lam = delta / prev_delta
                if 0.5 < lam < 1.0:
                    f = lam / (1.0 - lam)
                    for i, acts in enumerate(self.acts):
                        if acts is not None:
                            V[i] = min(penalty, V[i] + (V[i] - old[i]) * f)
                    since_jump, delta = 0, None
            prev_delta = delta
        self._vnr = [v if v < penalty / 10 else float("inf") for v in V]
        return self._vnr

    def options_at(self, s: State) -> list[dict]:
        """Every legal action at s with its expected total cost (Q-value) and outcome odds."""
        i = self.idx[s]
        rs = self.restart_cost + self.vstart
        here = self.V[i]
        vnr = self.no_restart_values()
        out = []
        for a, outs in self.acts[i] or []:
            q = a.cost + sum(p * (rs if j < 0 else self.V[j]) for p, j in outs)
            keep = sum(p for p, j in outs if j >= 0 and self.states[j][1] & s[1] == s[1])
            progress = sum(p for p, j in outs if j >= 0 and self.V[j] < here - 1e-9)
            finish = sum(p * (1 - self.R[j]) for p, j in outs if j >= 0)
            cont = q - a.cost
            no_restart = sum(p * (float("inf") if j < 0 else vnr[j]) for p, j in outs)
            out.append({"key": a.key, "label": a.label, "status": a.status, "action_cost": a.cost,
                        "expected_total": q, "p_progress": progress, "p_keep_targets": keep,
                        "p_finish_without_restart": finish, "continuation_cost": cont,
                        "cost_to_finish_this_item": None if no_restart == float("inf") else no_restart,
                        "outcomes": [(p, j) for p, j in outs]})
        out.append({"key": "restart", "label": "Start over (Orb of Scouring + fresh plan)", "status": "computed",
                    "action_cost": self.restart_cost, "expected_total": rs, "p_progress": 0.0,
                    "p_keep_targets": 0.0 if s[1] else 1.0, "p_finish_without_restart": 0.0,
                    "continuation_cost": self.vstart, "cost_to_finish_this_item": None, "outcomes": []})
        out.sort(key=lambda o: o["expected_total"])
        return out

    def usage(self) -> dict:
        """Expected uses per finished item of every action under the policy (from the d0 starts),
        plus the share of fresh attempts whose first move is each action."""
        n = len(self.states)
        x = [0.0] * n
        first: dict = defaultdict(float)
        for p, i in self.d0:
            x[i] += p
            if self.acts[i] is None:
                first["done"] += p
            elif self.policy[i] is None:
                first["restart"] += p
            else:
                first[self.acts[i][self.policy[i]][0].key] += p
        visits = [0.0] * n
        cur = x
        for _ in range(5000):
            nxt = [0.0] * n
            mass = 0.0
            for i, m in enumerate(cur):
                if m <= 0:
                    continue
                visits[i] += m
                acts = self.acts[i]
                if acts is None or self.policy[i] is None:
                    continue
                for p, j in acts[self.policy[i]][1]:
                    if j >= 0:
                        nxt[j] += m * p
                        mass += m * p
            cur = nxt
            if mass < 1e-12:
                break
        per_item = 1 / self.p_attempt if self.p_attempt > 0 else float("inf")
        acts_out: dict = {}
        for i, v in enumerate(visits):
            acts = self.acts[i]
            if v <= 0 or acts is None or self.policy[i] is None:
                continue
            a, outs = acts[self.policy[i]]
            here = self.A[i] + self.R[i] * self.vstart
            prog = sum(p for p, j in outs if j >= 0 and self.A[j] + self.R[j] * self.vstart < here - 1e-9)
            d = acts_out.setdefault(a.key, {"action": a, "uses": 0.0, "progress_mass": 0.0})
            d["uses"] += v * per_item
            d["progress_mass"] += v * per_item * prog
        for d in acts_out.values():
            d["p_progress"] = d["progress_mass"] / d["uses"] if d["uses"] else 0.0
            d["spend"] = d["uses"] * d["action"].cost
        return {"first": dict(first), "actions": acts_out}
