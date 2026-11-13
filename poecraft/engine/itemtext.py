"""Import an item from the game's own item text (Ctrl+C in Path of Exile; Ctrl+Alt+C "advanced" copy too).

Works on English and on the localized clients we have game data for (ru, de): base names and mod texts are matched
against the game's own strings by internal id (i18n.game_text), never translated. Everything that cannot be matched
is reported as a warning and skipped - an import never fails because of one unknown line.

Result (parse_item_text):
    {"base_id", "base_name", "item_class", "ilvl", "rarity", "influences": [...],
     "targets": [{"key", "signature", "label", "min", "value", "affix", "fractured"?, "eldritch"?}],
     "have": [{"id", "text", "affix"}],      # concrete mod ids for --have / recovery
     "lines": [{"text", "kind", "matched"}], "warnings": [str]}
What the client calls its header fields and line markers ("Item Class:", "(implicit)", "Shaper Item" ...) is UI text
of the game client, not part of the game data export: the English ones are the client's; the Russian / German ones
below are best-effort and are reported in the warnings when a section could not be recognised.
"""
from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache

from .. import config, i18n
from ..messages import G, J, M, N
from ..data.eldritch import get_eldritch
from ..data.repository import Base, GameData, get_data, get_rules
from . import eldritch as eld
from .context import ItemContext, generic_text, to_display
from .modsearch import defence_of

SEPARATOR = re.compile(r"^-{4,}$")
FIELD = {
    "item_class": re.compile(r"^(?:Item Class|Класс предмета|Gegenstandsklasse)\s*:\s*(.+)$", re.I),
    "rarity": re.compile(r"^(?:Rarity|Редкость|Seltenheit)\s*:\s*(.+)$", re.I),
    "ilvl": re.compile(r"^(?:Item Level|Уровень предмета|Gegenstandsstufe)\s*:\s*(\d+)", re.I),
}
RARITY = {"normal": "normal", "magic": "magic", "rare": "rare", "unique": "unique",
          "обычный": "normal", "волшебный": "magic", "редкий": "rare", "уникальный": "unique",
          "normal ": "normal", "magisch": "magic", "selten": "rare", "einzigartig": "unique"}
# influence / Eldritch footer lines ("Shaper Item", "Searing Exarch Item" ...): matched by keyword
INFLUENCE_WORDS = {
    "shaper": ("shaper item", "предмет создателя", "schöpfer"),
    "elder": ("elder item", "предмет древнего", "älteste"),
    "crusader": ("crusader item", "крестонос", "kreuzritter"),
    "hunter": ("hunter item", "предмет охотника", "jäger"),
    "redeemer": ("redeemer item", "избавител", "erlöser"),
    "warlord": ("warlord item", "предмет вождя", "kriegsherr"),
}
ELDRITCH_WORDS = {"exarch": ("searing exarch item", "экзарх", "exarch"),
                  "eater": ("eater of worlds item", "пожирател", "weltenfresser")}
MARKERS = {   # trailing "(...)" on a mod line
    "implicit": ("implicit", "неявн", "implizit"),
    "crafted": ("crafted", "создан", "hergestellt"),
    "fractured": ("fractured", "расколот", "zerbrochen"),
    "enchant": ("enchant", "зачарован", "verzauber"),
}
SKIP_LINE = re.compile(r"^(?:Corrupted|Осквернено|Verderbt|Mirrored|Отражено|Split|Unidentified|"
                       r"Fractured Item|Расколотый предмет|Zerbrochener Gegenstand|Synthesised Item|"
                       r"Синтезированный предмет|Synthetisierter Gegenstand|"
                       r"Неопознано|Note|Примечание|Notiz|Sockets|Гнёзда|Requirements|Требования|Anforderungen|"
                       r"Quality|Качество|Qualität|Level|Уровень|Stufe|Str|Dex|Int|Сил|Ловк|Инт)\b", re.I)
ADVANCED_RANGE = re.compile(r"(?<=\d)\((-?[\d.]+)-(-?[\d.]+)\)")      # "+80(70-84)" in Ctrl+Alt+C text
FIRST_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
HEADER = re.compile(r"^\{(.*)\}$")                                    # "{ Prefix Modifier "Prior's" (Tier: 3) — Life }"
# advanced-copy headers: "{ Searing Exarch Implicit Modifier (Lesser) ... }", "{ Собственное свойство Пожирателя миров
# (Мелкий) }", "{ Crafted Prefix Modifier ... }" / "{ Ремесленное Свойство-префикс ... }"
HEADER_IMPLICIT = ("implicit", "неявн", "собственное свойство", "implizit")
HEADER_CRAFTED = ("crafted", "ремесленн", "hergestellt")
HEADER_SIDES = {"exarch": ("searing exarch", "экзарх", "exarch"),
                "eater": ("eater of worlds", "пожирател", "weltenfresser", "verschlinger")}
DEFENCE_LINES = {"armour": ("armour", "броня", "rüstung"), "evasion": ("evasion", "уклонение", "ausweich"),
                 "energy_shield": ("energy shield", "энергетический щит", "energieschild"),
                 "ward": ("ward", "барьер", "schutz")}


# Eldritch "presence" upgrades: "While a Unique Enemy is in your Presence, <the same implicit>"
PRESENCE = re.compile(r"^[^,]*(presence|присутстви|präsenz|gegenwart)[^,]*,\s*", re.I)
# defence words inside a modifier's text (stems: Russian declines them - "брони", "энергетического щита")
MOD_DEFENCE_WORDS = {"armour": ("armour", "брон", "rüstung"), "evasion": ("evasion", "уклонени", "ausweich"),
                     "energy_shield": ("energy shield", "энергетическ", "energieschild")}
_UNITS = re.compile(r"\b(metre|second|minute|charge|time)s\b")
FUZZY_CUTOFF = 0.85                  # difflib ratio for an Eldritch implicit that differs slightly from the game text


def _loose(s: str) -> str:
    """A forgiving key: case, ё/е, punctuation, singular/plural units and doubled spaces do not matter."""
    s = _norm(s).replace("ё", "е")
    s = _UNITS.sub(lambda m: m.group(1), s)
    s = re.sub(r"[^\w#%+\-\n ]", " ", s)
    return re.sub(r"[ ]+", " ", s).strip()


def _lookup_eldritch(table: dict, text: str) -> tuple[list | None, str | None]:
    """(entries, matched game text or None) for an Eldritch implicit line: exact, then loose, then fuzzy. The game
    text comes back only for an approximate match, so the caller can say what it was matched to."""
    key = _norm(generic_text(text))
    if key in table:
        return table[key], None
    loose = {}
    for k in table:
        loose.setdefault(_loose(k), k)
    k = loose.get(_loose(key))
    if k:
        return table[k], None
    close = difflib.get_close_matches(_loose(key), list(loose), n=1, cutoff=FUZZY_CUTOFF)
    if close:
        return table[loose[close[0]]], loose[close[0]]
    return None, None


def _base_conflict(base: Base, text: str) -> list[str]:
    """Defences a modifier's text needs that the base does not have ('Armour and Energy Shield' on an ES-only base)."""
    low = _norm(text)
    has = set(defence_of(base))
    return [d for d, words in MOD_DEFENCE_WORDS.items() if any(w in low for w in words) and d not in has]


def _defence_names(defs) -> J:
    return J([M(f"def.{d}") for d in defs] or [M("def.none")], " / ")


def _unordered(key: str) -> str:
    """A multi-line (hybrid) mod key with its lines sorted: the item lists a hybrid's lines in stat order, which need
    not be the order of the game data's text ("+1 to Level of Socketed AoE Gems" above "8% increased Area of
    Effect", stored the other way round)."""
    sep = " / " if " / " in key else "\n"            # generic_text joins a hybrid's lines with " / "
    return sep.join(sorted(key.split(sep))) if sep in key else key


# item status lines; the Russian / German stems are GGG's own words (the localized trade sites' filter labels
# "Осквернённый" / "Отзеркаленный", "Verderbt" / "Gespiegelt") - the item line may use another form of the word
STATUS_WORDS = {"corrupted": ("corrupted", "осквернен", "осквернён", "verderbt"),
                "mirrored": ("mirrored", "отзеркален", "gespiegelt")}
# rarity words of items the planner cannot craft (no base in the crafting data by design)
NOT_CRAFTABLE = {"currency": ("currency", "валюта", "währung"),
                 "divination": ("divination card", "гадальная карта", "weissagungskarte"),
                 "gem": ("gem", "камень", "gemme"),
                 "unique": ("unique", "уникальн", "einzigartig")}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _marker(line: str) -> tuple[str, str]:
    """('+80 to maximum Life', 'fractured') for '+80 to maximum Life (fractured)'."""
    m = re.search(r"\s*\(([^()]*)\)\s*$", line)
    if m:
        word = m.group(1).lower()
        for kind, words in MARKERS.items():
            if any(word.startswith(w) for w in words):
                return line[:m.start()].rstrip(), kind
    return line, "explicit"


def _clean(line: str) -> str:
    line = ADVANCED_RANGE.sub("", line)
    return re.sub(r"\s+[—-]\s+Unscalable Value$", "", line).strip()


def _sections(text: str) -> list[list[str]]:
    out, cur = [], []
    for raw in text.replace("\r", "").split("\n"):
        line = raw.strip()
        if SEPARATOR.match(line):
            out.append(cur)
            cur = []
        elif line:
            cur.append(line)
    out.append(cur)
    return [s for s in out if s]


def _find_base(data: GameData, lines: list[str], item_class: str | None, defences: set) -> Base | None:
    """The base named in the header lines: exact name first (English or the game's localized name), then the
    longest name contained in a line (magic items carry affix words around it). Several bases can share a name
    (e.g. Two-Toned Boots): the one whose defence properties match the item's property lines wins."""
    names: dict[str, list[Base]] = {}
    classes = {b.item_class.lower() for b in data.bases.values()}
    # an English "Item Class:" narrows the search; a localized class name is not in the game data, so it cannot
    for b in data.bases.values():
        if item_class and item_class.lower() in classes and b.item_class.lower() != item_class.lower():
            continue
        for lang in ("en", "ru", "de"):
            n = b.name if lang == "en" else i18n.game_text("base", b.id, b.name, lang)[0]
            names.setdefault(_norm(n), []).append(b)
    cands: list[Base] = []
    for line in lines:
        cands = names.get(_norm(line), [])
        if cands:
            break
    if not cands:
        best = ""
        for line in lines:
            low = _norm(line)
            for n in names:
                if len(n) > len(best) and re.search(rf"(^|\s){re.escape(n)}($|\s)", low):
                    best = n
        cands = names.get(best, [])
    if not cands:
        return None
    cands = sorted(set(cands), key=lambda b: (set(defence_of(b)) != defences if defences else False, -b.drop_level))
    return cands[0]


class _Index:
    """generic text (English and localized) of every mod that can be a target on this base -> family info."""

    def __init__(self, data: GameData, ctx: ItemContext):
        self.by_text: dict[str, list[dict]] = {}
        for f in ctx.families().values():
            entry = {"key": f.signature, "signature": f.signature, "affix": f.affix, "label": f.label, "stat": f.primary_stat,
                     "tiers": [t for t in f.tiers]}
            for t in f.tiers:
                self._add(t.get("text"), t.get("mod_id"), entry)
        self.eldritch: dict[str, list[dict]] = {}
        if not ctx.influences:
            for key, fam in eld.base_families(get_eldritch(), ctx.base.item_class, ctx.tags).items():
                entry = {"key": fam.key, "signature": fam.key, "affix": fam.side, "label": fam.label, "stat": fam.primary_stat,
                         "eldritch": fam.side,
                         "tiers": [{"mod_id": m.id, "min": m.stats[0].min, "max": m.stats[0].max, "label": f"T{t}",
                                    "source": "eldritch", "text": m.text} for t, m in sorted(fam.tiers.items())]}
                for m in fam.tiers.values():
                    for lang in ("en", "ru", "de"):
                        txt = m.text if lang == "en" else i18n.game_text("mod", m.id, m.text, lang)[0]
                        self.eldritch.setdefault(_norm(generic_text(txt)), [])
                        if entry not in self.eldritch[_norm(generic_text(txt))]:
                            self.eldritch[_norm(generic_text(txt))].append(entry)

    def _add(self, text, mod_id, entry):
        if not text:
            return
        for lang in ("en", "ru", "de"):
            txt = text if lang == "en" else i18n.game_text("mod", mod_id, text, lang)[0]
            key = _norm(generic_text(txt))
            for k in {key, _unordered(key)}:      # hybrids also under their lines in any order
                lst = self.by_text.setdefault(k, [])
                if entry not in lst:
                    lst.append(entry)


def _value(lines: list[str]) -> float | None:
    m = FIRST_NUMBER.search(" ".join(lines))
    return float(m.group()) if m else None


def _tier_for(entry: dict, value: float | None, tier_no: int | None) -> dict | None:
    """The rollable tier the item's value belongs to. The value decides; the tier number of an advanced copy only
    breaks ties (the game numbers tiers per item class, the planner per base, so they need not agree)."""
    rolls = [t for t in entry["tiers"] if t.get("source") in ("roll", "eldritch") and t.get("mod_id")]
    by_no = next((t for t in rolls if tier_no is not None and t.get("label") == f"T{tier_no}"), None)
    if value is None:
        return by_no or (rolls[0] if rolls else None)
    fits = [t for t in rolls if t.get("min") is not None and t["min"] <= value <= t["max"]]
    if by_no in fits:
        return by_no
    # the narrowest range that holds the value (the export also carries wide event-only "Royale" variants)
    fits.sort(key=lambda t: ("Royale" in t["mod_id"], t["max"] - t["min"]))
    return fits[0] if fits else by_no


def _groups(body: list[str]) -> list[tuple[list[str], str, int | None, str | None]]:
    """Mod groups of one section: (lines, kind, tier number, Eldritch side). An advanced-copy header groups the lines
    under it (one mod, also when it is a hybrid) and says what they are - an Eldritch implicit (and its side), a
    crafted mod, an implicit or an explicit; plain text gives one group per line, its kind from the line marker."""
    groups = []
    i = 0
    while i < len(body):
        hm = HEADER.match(body[i])
        if hm:
            h = hm.group(1).lower()
            tier = re.search(r"\(\D*?(\d+)\)", h)
            j = i + 1
            while j < len(body) and not HEADER.match(body[j]):
                j += 1
            side = next((sd for sd, words in HEADER_SIDES.items() if any(w in h for w in words)), None)
            if side or any(w in h for w in HEADER_IMPLICIT):
                kind = "implicit"
            elif any(w in h for w in HEADER_CRAFTED):
                kind = "crafted"
            else:
                kind = _marker(body[i + 1])[1] if i + 1 < j else "explicit"
            groups.append(([_marker(x)[0] for x in body[i + 1:j]], kind, int(tier.group(1)) if tier else None, side))
            i = j
            continue
        line, kind = _marker(body[i])
        groups.append(([line], kind, None, None))
        i += 1
    return groups


def _bounds(text: str) -> tuple[float, float] | None:
    """(low, high) of the first number of a mod text as the game displays it: '(0.2-0.4)%' -> (0.2, 0.4)."""
    m = re.search(r"\((-?[\d.]+)-(-?[\d.]+)\)|(-?\d+(?:\.\d+)?)", text)
    if not m:
        return None
    if m.group(1) is not None:
        return float(m.group(1)), float(m.group(2))
    return float(m.group(3)), float(m.group(3))


def _tier_by_text(entry: dict, value: float | None, kind: str) -> dict | None:
    """The tier whose displayed range (its own text, in any language we have) holds the item's value. Needed where
    the game shows a stat in other units than it stores it ('1.2 m' = 12; '0.2%' leech = 200 per 100 000)."""
    if value is None:
        return None
    sources = ("roll", "eldritch") + (("bench",) if kind == "crafted" else ())
    fits = []
    for t in entry["tiers"]:
        if t.get("source") not in sources or not t.get("mod_id") or not t.get("text"):
            continue
        for lang in ("en", "ru", "de"):
            txt = t["text"] if lang == "en" else i18n.game_text("mod", t["mod_id"], t["text"], lang)[0]
            b = _bounds(txt)
            if b and b[0] <= value <= b[1]:
                fits.append((("Royale" in t["mod_id"], b[1] - b[0]), t))
                break
    fits.sort(key=lambda x: x[0])
    return fits[0][1] if fits else None


_KNOWN: dict = {}


def _base_implicit(data: GameData, base: Base, text: str) -> bool:
    """Is this line (generic form, any language) one of the base's own implicits?"""
    key = _norm(generic_text(text))
    texts = _implicit_texts()
    for mid in base.implicits:
        en = texts.get(mid)
        if en and any(_norm(generic_text(i18n.game_text("mod", mid, en, lang)[0] if lang != "en" else en)) == key
                      for lang in ("en", "ru", "de")):
            return True
    return False


def _eldritch_english(text: str) -> str | None:
    """The English generic text ("#% increased Fire Damage") of an Eldritch implicit line in any language - from every
    Eldritch implicit in the game data, whatever item class it is for."""
    return _eldritch_texts().get(_norm(generic_text(text)))


@lru_cache(maxsize=1)
def _eldritch_texts() -> dict:
    try:
        mods = json.loads((config.RAW_DIR / "mods.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for mid, m in mods.items():
        if m.get("generation_type") in ("searing_exarch_implicit", "eater_of_worlds_implicit") and m.get("text"):
            en = generic_text(m["text"])
            for lang in ("en", "ru", "de"):
                txt = m["text"] if lang == "en" else i18n.game_text("mod", mid, m["text"], lang)[0]
                out.setdefault(_norm(generic_text(txt)), en)
    return out


@lru_cache(maxsize=1)
def _implicit_texts() -> dict:
    """mod id -> English text of every base implicit (they are not rollable, so not in the crafting database)."""
    try:
        bases = json.loads((config.RAW_DIR / "base_items.json").read_text(encoding="utf-8"))
        wanted = {m for b in bases.values() for m in (b.get("implicits") or [])}
        mods = json.loads((config.RAW_DIR / "mods.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {mid: mods[mid]["text"] for mid in wanted if mid in mods and mods[mid].get("text")}


def _status(flat: list[str]) -> set:
    """"corrupted" / "mirrored" from the item's status line (English, or GGG's Russian / German trade-site words)."""
    found = set()
    for line in flat:
        low = _norm(line)
        if len(low) > 30:
            continue
        for st, stems in STATUS_WORDS.items():
            if any(low.startswith(w) for w in stems):
                found.add(st)
    return found


def _known_anywhere(data: GameData, text: str) -> bool:
    """Is this line (generic form) some modifier's text in the game data at all - in any language we have?"""
    if not _KNOWN:
        for m in data.mods.values():
            for lang in ("en", "ru", "de"):
                txt = m.text if lang == "en" else i18n.game_text("mod", m.id, m.text, lang)[0]
                if txt:
                    _KNOWN[_norm(generic_text(txt))] = True
    return _norm(generic_text(text)) in _KNOWN


def parse_item_text(text: str, data: GameData | None = None, rules: dict | None = None) -> dict:
    data, rules = data or get_data(), rules or get_rules()
    warnings: list[str] = []
    sections = _sections(text)
    if not sections:
        return {"warnings": [M("m.imp.empty")], "targets": [], "have": [], "lines": []}
    flat = [l for s in sections for l in s]
    item_class = rarity = None
    ilvl = None
    for line in flat:
        if (m := FIELD["item_class"].match(line)) and item_class is None:
            item_class = m.group(1).strip()
        elif (m := FIELD["rarity"].match(line)) and rarity is None:
            rarity = RARITY.get(m.group(1).strip().lower(), m.group(1).strip().lower())
        elif (m := FIELD["ilvl"].match(line)) and ilvl is None:
            ilvl = int(m.group(1))
    # header lines: the first section without the Item Class / Rarity fields (or, without separators, the first lines)
    head = [l for l in sections[0] if not (FIELD["item_class"].match(l) or FIELD["rarity"].match(l))][:3]
    defences = {d for d, words in DEFENCE_LINES.items() for l in flat
                if any(_norm(l).startswith(w) for w in words) and ":" in l}
    base = _find_base(data, head, item_class, defences) or _find_base(data, flat[:6], item_class, defences)
    status = _status(flat)
    if base is None:
        # say why: a card / currency / gem / unique is not something to craft; anything else is an unknown base
        what = next((k for k, words in NOT_CRAFTABLE.items() if rarity and any(str(rarity).startswith(w) for w in words)),
                    None)
        warn = M(f"m.imp.not_craftable_{what}", lines=" / ".join(head)) if what else M("m.imp.no_base", lines=" / ".join(head))
        return {"warnings": [warn], "targets": [], "have": [], "lines": [], "item_class": item_class, "rarity": rarity,
                "ilvl": ilvl, "status": sorted(status)}
    if ilvl is None:
        warnings.append(M("m.imp.no_ilvl"))
        ilvl = 86
    influences = [inf for inf, words in INFLUENCE_WORDS.items()
                  if any(any(w in _norm(l) for w in words) and len(l) < 40 for l in flat)]
    sides = {side for side, words in ELDRITCH_WORDS.items() if any(any(w in _norm(l) for w in words) and len(l) < 40
                                                                   for l in flat)}
    out = {"base_id": base.id, "base_name": base.name, "item_class": base.item_class, "ilvl": ilvl, "rarity": rarity,
           "influences": influences[:rules["max_influences"]], "targets": [], "have": [], "lines": [],
           "warnings": warnings, "status": sorted(status), "extra_implicits": []}
    for st in sorted(status):            # a corrupted / mirrored item cannot be changed: the plan is for a fresh base
        warnings.append(M(f"m.imp.{st}"))
    base_defs = set(defence_of(base))
    if defences and base_defs and not defences & base_defs:
        # e.g. "Armour: 300" on Warlock Boots (Energy Shield only): the name or the properties were miscopied
        warnings.append(M("m.imp.base_defence_mismatch", base=G("base", base.id, base.name),
                          lines=_defence_names(sorted(defences)), has=_defence_names(defence_of(base))))
    if rarity == "unique":
        warnings.append(M("m.imp.unique"))
        return out
    ctx = ItemContext(data, rules, base, ilvl, tuple(out["influences"]))
    index = _Index(data, ctx)
    # mod lines: everything after the Item Level line (or after the header, for text without one)
    start = next((i for i, l in enumerate(flat) if FIELD["ilvl"].match(l)), len(head) - 1) + 1
    keep = lambda l: not SKIP_LINE.match(l) and not any(
        any(w in _norm(l) for w in ws) and len(l) < 40 for ws in list(INFLUENCE_WORDS.values()) + list(ELDRITCH_WORDS.values()))
    seen_lines, bodies = 0, []
    for sec in sections:                      # the sections from the mod part on, filtered
        lo = max(0, start - seen_lines)
        seen_lines += len(sec)
        part = [l for l in sec[lo:] if keep(l)] if lo < len(sec) else []
        if part:
            bodies.append(part)
    groups: list[tuple[list[str], str, int | None]] = []     # (lines, kind, tier) - advanced copy groups by header
    for body in bodies:
        groups += _groups(body)
    used_sides: set = set()
    fractured_seen = False
    seen_keys: set = set()
    k = 0
    while k < len(groups):
        lines, kind, tier, side = groups[k]
        if kind == "enchant":
            out["lines"].append({"text": " / ".join(lines), "kind": kind, "matched": False})
            k += 1
            continue
        lines = [_clean(x) for x in lines]
        if kind == "implicit" and side is None and not sides and _base_implicit(data, base, "\n".join(lines)):
            # the base's own implicit (Astral Plate's "+12% to all Elemental Resistances"): not an Eldritch implicit
            # even when an Eldritch implicit has the same text - the item would show its Eldritch line
            out["lines"].append({"text": " / ".join(lines), "kind": kind, "matched": False})
            k += 1
            continue
        presence = kind == "implicit" and bool(PRESENCE.match(lines[0]))
        if presence:
            lines = [PRESENCE.sub("", lines[0], count=1)] + lines[1:]
        entry, take, approx = None, 1, None
        table = index.eldritch if kind == "implicit" else index.by_text
        # a hybrid mod spans two lines in plain Ctrl+C text: try the pair first
        if tier is None and side is None and k + 1 < len(groups) and groups[k + 1][1] == kind \
                and groups[k + 1][2] is None and groups[k + 1][3] is None:
            pair = [lines[0], _clean(groups[k + 1][0][0])]
            key = _norm(generic_text("\n".join(pair)))
            cand = table.get(key) or table.get(_unordered(key))
            if cand:
                entry, lines, take = cand, pair, 2
        if entry is None:
            key = _norm(generic_text("\n".join(lines)))
            entry = table.get(key) or (table.get(_unordered(key)) if len(lines) > 1 else None)
        if entry is None and kind == "implicit" and (side or presence or sides):
            # an Eldritch implicit worded slightly differently from the game data (another client version, a
            # translation, a typo): loose, then fuzzy matching - only when the item is known to be Eldritch
            entry, approx = _lookup_eldritch(table, "\n".join(lines))
        text_shown = " / ".join(lines)
        if not entry and kind == "implicit" and (side or sides or presence) \
                and not _base_implicit(data, base, "\n".join(lines)):
            # the item says this is an Eldritch implicit (its header or its "Searing Exarch Item" line), but the data
            # has no such Eldritch implicit for this base: keep it as one - for the price search (the trade site's
            # implicit stat) - instead of checking it against the explicit modifiers and calling it impossible
            where = side or (next(iter(sides)) if len(sides) == 1 else None)
            out["extra_implicits"].append({"text": text_shown, "side": where, "value": _value(lines),
                                           "english": _eldritch_english("\n".join(lines))})
            warnings.append(M("m.imp.eld_unlisted", line=text_shown))
            out["lines"].append({"text": text_shown, "kind": kind, "matched": False, "eldritch": where})
            k += take
            continue
        if not entry:
            if kind == "implicit" and side is None:
                out["lines"].append({"text": text_shown, "kind": kind, "matched": False})   # base implicit: not a target
            elif _known_anywhere(data, "\n".join(lines)):
                # a real modifier, just not one this base can have (e.g. a local Armour/ES craft on an ES-only base):
                # when its defences contradict the base, say which - the base name or this line was miscopied
                need = _base_conflict(base, "\n".join(lines))
                if need:
                    warnings.append(M("m.imp.not_on_base_defence", line=text_shown, base=G("base", base.id, base.name),
                                      has=_defence_names(defence_of(base)), needs=_defence_names(need)))
                else:
                    warnings.append(M("m.imp.not_on_base", line=text_shown))
                out["lines"].append({"text": text_shown, "kind": kind, "matched": False})
            else:
                warnings.append(M("m.imp.unknown", line=text_shown))
                out["lines"].append({"text": text_shown, "kind": kind, "matched": False})
            k += take
            continue
        if approx:
            warnings.append(M("m.imp.approx", line=text_shown, match=approx))
        if kind == "implicit":       # Eldritch implicit: pick the side the item shows (or the first free one)
            if presence and any(e["key"] in seen_keys for e in entry):
                # the presence upgrade of an implicit already read: part of it, not another target
                out["lines"].append({"text": text_shown, "kind": "presence", "matched": True, "key": entry[0]["key"]})
                k += take
                continue
            allowed = {side} if side else (sides or {"exarch", "eater"})     # the header names the side, if any
            choices = [e for e in entry if e["eldritch"] in allowed and e["eldritch"] not in used_sides]
            if not choices:
                # the game data has this implicit on the other side than the header / item says: the modifier text
                # is what identifies it, so use the data's side and say so (never drop the line silently)
                choices = [e for e in entry if e["eldritch"] not in used_sides]
                if choices:
                    warnings.append(M("m.imp.eld_side", line=text_shown,
                                      said=N(eld.SIDE_NAMES[side or next(iter(allowed))]),
                                      data=N(eld.SIDE_NAMES[choices[0]["eldritch"]])))
            if not choices:
                warnings.append(M("m.imp.duplicate", line=text_shown))
                out["lines"].append({"text": text_shown, "kind": kind, "matched": False})
                k += take
                continue
            if len(choices) > 1 and not sides and not side:
                warnings.append(M("m.imp.eld_ambiguous", side=N(eld.SIDE_NAMES[choices[0]["eldritch"]]), line=text_shown))
            e = choices[0]
            used_sides.add(e["eldritch"])
        else:
            e = entry[0]
        value = _value(lines)
        t = _tier_by_text(e, value, kind) or _tier_for(e, value, tier)
        if e["key"] in seen_keys:
            warnings.append(M("m.imp.duplicate", line=text_shown))
            k += take
            continue
        seen_keys.add(e["key"])
        target = {"key": e["key"], "signature": e["signature"], "label": e["label"], "affix": e["affix"],
                  # thresholds in the units the game displays (the planner converts them to stored units)
                  "value": value, "min": (to_display(e.get("stat"), t["min"]) if t and t.get("min") is not None else value)}
        if e.get("eldritch"):
            target["eldritch"] = e["eldritch"]
        if kind == "fractured":
            if fractured_seen:
                warnings.append(M("m.imp.two_fractured", line=text_shown))
            else:
                target["fractured"] = True
                fractured_seen = True
        out["targets"].append(target)
        if kind == "crafted":
            warnings.append(M("m.imp.crafted", line=text_shown))
        elif t and t.get("mod_id") and not e.get("eldritch") and kind != "fractured":
            out["have"].append({"id": t["mod_id"], "text": text_shown, "affix": e["affix"]})
        elif kind == "fractured":
            warnings.append(M("m.imp.fractured_have", line=text_shown))
        elif e.get("eldritch"):
            warnings.append(M("m.imp.eldritch_have", line=text_shown))
        out["lines"].append({"text": text_shown, "kind": kind, "matched": True, "key": e["key"]})
        k += take
    return out
