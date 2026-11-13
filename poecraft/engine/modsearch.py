"""Mod search for the target picker, and why a mod is missing from the selected base.

matches:   families of the selected base whose text contains every word of the query, in any order (localized
           label, English label, tier texts), closest first. Word endings are dropped (inflected languages), the
           usual player abbreviations expand (es, ms, hp, res).
elsewhere: stat lines of the same item class that match the query but cannot roll on this base. Everything about
           them comes from the game data: a stat line "rolls on" a base when one of its mods has a spawn weight > 0
           for that base's own tags (no influence), exactly as ItemContext builds the pool. What a base type
           "requires" is the set of defence properties (armour / evasion / energy_shield / ward) shared by every
           base of the class that can roll it, read from base_items properties.
"""
from __future__ import annotations

import re

from .. import i18n
from ..data.repository import Base, GameData, get_rules
from .context import event_only, generic_text

STOP = {"and", "to", "of", "the", "a", "an", "и", "к", "в", "на", "und", "zu", "von", "der", "die", "das", "mit"}
ALIASES = {"es": "energy shield", "ms": "movement speed", "hp": "life", "res": "resist"}
DEFENCES = ("armour", "evasion", "energy_shield", "ward")


def stem(w: str) -> str:
    return w[:max(4, len(w) - 2)] if len(w) > 4 else w


def search_words(q: str) -> list[str]:
    words = [w for w in re.split(r"[\s/,&+#%()]+", q.lower()) if w and w not in STOP]
    return [stem(x) for w in words for x in ALIASES.get(w, w).split(" ")]


def _hit(words: list[str], texts) -> bool:
    hay = "\n".join(t for t in texts if t).lower()
    return all(w in hay for w in words)


def _rank(q: str, labels) -> int:
    labels = [x.lower() for x in labels if x]
    if any(x.lstrip("+#% ").startswith(q) for x in labels):
        return 0
    return 1 if any(q in x for x in labels) else 2


def defence_of(base: Base) -> tuple[str, ...]:
    """The base's defence types, from its properties in the game data (empty for jewellery, weapons ...)."""
    return tuple(d for d in DEFENCES if d in (base.properties or {}))


_INDEX: dict = {}


def _class_index(data: GameData, item_class: str) -> dict:
    """signature -> {"bases": {base ids that can roll it, own tags only}, "mod": a representative mod}."""
    key = (id(data), item_class)
    if key not in _INDEX:
        _INDEX[key] = _build_index(data, item_class)
    return _INDEX[key]


def _build_index(data: GameData, item_class: str) -> dict:
    rules = get_rules()
    bases = [b for b in data.bases.values() if b.item_class == item_class]
    out: dict = {}
    for m in data.mods.values():
        if m.domain != "item" or m.essence_only or m.affix not in ("prefix", "suffix") or event_only(m, rules):
            continue
        rolls = [b.id for b in bases if m.spawn_tag_weight(set(b.tags))[1] * m.generation_multiplier(set(b.tags)) > 0]
        if not rolls:
            continue
        e = out.setdefault(m.signature, {"bases": set(), "mod": m})
        e["bases"].update(rolls)
        if m.required_level < e["mod"].required_level:
            e["mod"] = m
    return out


def _shared_name(data: GameData, base: Base) -> bool:
    return sum(1 for b in data.bases.values() if b.name == base.name and b.item_class == base.item_class) > 1


def mod_search(data: GameData, base: Base, families: list[dict], q: str, lang: str = i18n.DEFAULT) -> dict:
    """See the module docstring. `families` is the selected base's family list as the UI shows it."""
    q = q.strip().lower()
    words = search_words(q)
    if not words:
        return {"query": q, "matches": [f.get("key") or f["signature"] for f in families], "elsewhere": [],
                "base_defence": list(defence_of(base))}
    hits = [f for f in families
            if _hit(words, [f["label"], f.get("label_en"), *(t.get("text") for t in f.get("tiers", []))])]
    hits.sort(key=lambda f: _rank(q, [f["label"], f.get("label_en")]))
    here = {f["signature"] for f in families}
    elsewhere = []
    for sig, e in _class_index(data, base.item_class).items():
        if sig in here:
            continue
        m = e["mod"]
        label_en = generic_text(m.text)
        label, ok = i18n.game_label(m.id, label_en, m.text, lang)
        if not _hit(words, [label_en, label]):
            continue
        others = [data.bases[b] for b in e["bases"]]
        kinds = {defence_of(b) for b in others}
        common = set(DEFENCES)
        for k in kinds:
            common &= set(k)
        # examples: bases whose name is unambiguous in this class first (several bases share a name, e.g. the
        # Two-Toned Boots exist as Armour/ES, Armour/Evasion and Evasion/ES versions)
        examples = sorted(others, key=lambda b: (_shared_name(data, b), -b.drop_level, b.name))[:3]
        elsewhere.append({
            "signature": sig, "affix": m.affix, "label": label, "label_en": label_en,
            # defence types every base that can roll it has; when they share none, each combination
            "requires": [d for d in DEFENCES if d in common],
            "requires_any": [] if common else sorted([list(k) for k in kinds if k]),
            "examples": [i18n.game_text("base", b.id, b.name, lang)[0] for b in examples],
            "_en": [] if ok else ["label"],
        })
    elsewhere.sort(key=lambda x: (_rank(q, [x["label"], x["label_en"]]), x["label_en"]))
    return {"query": q, "matches": [f.get("key") or f["signature"] for f in hits], "elsewhere": elsewhere[:12],
            "base_defence": list(defence_of(base))}
