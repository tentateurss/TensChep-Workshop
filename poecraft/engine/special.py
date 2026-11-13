"""Special fossils and Beastcrafting: what the game data supports, and the pool of a special-fossil reforge.

A special fossil is supported only when every effect in its export description is one the planner can model from
data (rules 'fossil_effects'); otherwise it is "not supported" with the missing piece named. Beastcrafting needs
recipes, which the export does not contain, so it is always "not supported (missing recipe data)".
"""
from __future__ import annotations

from ..data.special import SpecialData, SpecialFossil
from ..messages import CUR, Cap, J, M
from .context import PoolEntry

SUPPORTED = "supported"
MISSING = "not supported (missing fossil mod data)"
NO_TARGET = "not supported (no stat target)"


STATUS_TEXT = {MISSING: "m.status.missing_fossil", NO_TARGET: "m.status.no_target"}


def verdict(f: SpecialFossil, rules: dict) -> tuple[str, object, list]:
    """(status, reason, assumptions) for one special fossil, from its own export entry. The status is a fixed
    string; reason and assumptions are messages (rules 'fossil_effects' keys -> "m.fx.<key>")."""
    effects = rules["fossil_effects"]
    missing, no_target, assumptions = [], [], []
    for key, text in f.descriptions:
        e = effects.get(key)
        msg = M(f"m.fx.{key}", fossil=CUR(f.name))
        if e is None:
            missing.append(M("m.fx.unknown", text=text))
        elif "missing" in e:
            missing.append(msg)
        elif "no_target" in e:
            no_target.append(msg)
        elif "assumption" in e:
            assumptions.append(msg)
    if missing:
        return MISSING, J(missing, "; "), assumptions
    if no_target:
        return NO_TARGET, J(no_target, "; "), assumptions
    if not (f.added or f.forced):
        return NO_TARGET, M("m.fx.no_mod"), assumptions
    return SUPPORTED, "", assumptions


def feature_rows(data: SpecialData, rules: dict) -> list[dict]:
    """One feature row per special fossil and one for Beastcrafting, each with its own status and reason."""
    rows = []
    for name in rules["fossils_not_modelled"]:
        f = data.fossils.get(name)
        if f is None:
            rows.append({"name": CUR(name), "status": MISSING, "note": M("m.feat.not_found")})
            continue
        status, reason, assumptions = verdict(f, rules)
        if status == SUPPORTED:
            rows.append({"name": M("m.feat.special_supported", fossil=CUR(name), short=name.split()[0]),
                         "status": "approximate",
                         "note": J(assumptions, " ") if assumptions else M("m.feat.odds_from_data")})
        else:
            rows.append({"name": CUR(name), "status": status, "note": M("m.feat.dot", text=Cap(reason))})
    rows.append({"name": M("m.feat.beastcrafting"), "status": "not supported (missing recipe data)",
                 "note": M("m.feat.beast_note")})
    return rows


def pool(ctx, combo, special: SpecialFossil, divisor: float) -> list[PoolEntry]:
    """Reforge pool with `combo` (repository Fossils, the special one included): the normal pool with every
    fossil's tag weights, plus the special fossil's added modifiers at their spawn weights with the same tag
    weights applied (an assumption - see rules 'fossil_effects')."""
    out = list(ctx.pool(tuple(combo)))
    tags = set(ctx.tags)
    for m in special.added:
        if m.required_level > ctx.ilvl:
            continue
        w = float(m.spawn_weight(tags))
        for f in combo:
            for tag, fw in f.weights:
                if tag in m.tags:
                    w *= fw / divisor
        if w > 0:
            out.append(PoolEntry(m, w, 1))
    return out
