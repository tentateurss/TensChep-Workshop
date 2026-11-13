"""Localization.

Two separate concerns live here, on purpose:

1. UI strings (buttons, labels, headings, table headers, status words, CLI chrome). They are written by this
   project, so translating them is fine. They live in poecraft/locales/<code>.json - one flat file per language,
   key -> text with {placeholders}, plus "_meta": {"name": ..., "flag": ...}. Adding a language = adding a file;
   no code changes. A key missing from a language falls back to English and is MARKED (CLI: "[EN] " prefix, web
   UI: an "EN" badge) - never shown half-translated without a marker.

2. Game-data strings (base item names, mod text, essence / fossil / currency names). These must NEVER be
   machine-translated or guessed. They come from the game's own localized files (RePoE per-language exports,
   built into data/locale_game_<code>.json by `python -m poecraft update`), looked up by internal id in
   `game_text` below - the single integration point. Missing ids stay English, marked "EN".

3. Plan explanations (step details, notes, infeasibility reasons, warnings, strategy names) are message keys
   ("m.*") in the same locale files, built by the planner as language-free message objects and rendered at the
   edge - see poecraft/messages.py. Game-data names inside them are game_text() parameters.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
DEFAULT = "en"
FALLBACK_MARK = "[EN] "


@lru_cache(maxsize=None)
def _load(code: str, root: Path = LOCALES_DIR) -> dict:
    path = root / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def languages(root: Path = LOCALES_DIR) -> list[dict]:
    """Every locale file found in `root`: [{code, name, flag}], English first."""
    out = []
    for p in sorted(root.glob("*.json")):
        meta = _load(p.stem, root).get("_meta", {})
        out.append({"code": p.stem, "name": meta.get("name", p.stem), "flag": meta.get("flag", p.stem.upper())})
    out.sort(key=lambda x: (x["code"] != DEFAULT, x["code"]))
    return out


def strings(code: str, root: Path = LOCALES_DIR) -> dict:
    return {k: v for k, v in _load(code, root).items() if not k.startswith("_")}


def lookup(code: str, key: str, root: Path = LOCALES_DIR) -> tuple[str, bool]:
    """(text template, fell_back_to_english). An unknown key returns the key itself, marked as a fallback."""
    if code != DEFAULT:
        text = _load(code, root).get(key)
        if isinstance(text, str):
            return text, False
    text = _load(DEFAULT, root).get(key)
    return (text if isinstance(text, str) else key), code != DEFAULT or not isinstance(text, str)


def t(code: str, key: str, root: Path = LOCALES_DIR, /, **kw) -> str:
    """UI string for `key` in language `code`, formatted with kw; English fallback carries FALLBACK_MARK."""
    text, fell_back = lookup(code, key, root)
    try:
        text = text.format(**kw)
    except (KeyError, IndexError, ValueError):
        pass
    return (FALLBACK_MARK + text) if fell_back else text


# ====================================================================== game data
# Source: data/locale_game_<code>.json - {internal id: localized string} built by `python -m poecraft update`
# (poecraft/data/locales.py) from RePoE's per-language exports of the game's own localized files, validated
# against the English export of the same build. Ids: base item / essence / fossil / currency metadata path ->
# name; mod id -> mod text. Never machine-translated, never guessed: an id with no entry stays English and is
# reported as not localized (the UI marks it "EN", the CLI prefixes "[EN] ").

def _data_dir() -> Path:
    from . import config
    return config.DATA_DIR


@lru_cache(maxsize=None)
def _game_map(lang: str, root: Path | None = None) -> dict:
    path = (root or _data_dir()) / f"locale_game_{lang}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def game_data_status(lang: str, root: Path | None = None) -> dict:
    """Is a game-data locale file present for `lang`, and what does it cover?"""
    if lang == DEFAULT:
        return {"available": True, "language": "English"}
    meta = _game_map(lang, root).get("_meta")
    if not meta:
        try:
            report = json.loads(((root or _data_dir()) / "compatibility.json").read_text(encoding="utf-8"))
            source = report.get("sources", {}).get("locale_" + lang, {})
        except (OSError, ValueError):
            source = {}
        return {"available": False, "compatibility": source.get("compatibility", "missing"),
                "warning": source.get("validation", "English fallback") }
    return {"available": True, "language": meta.get("language"), "base_items": meta.get("base_items"),
            "mods": meta.get("mods"), "same_build_as": meta.get("same_build_as"),
            "excluded_incomplete_mod_texts": len(meta.get("excluded_incomplete_mod_texts", []))}


def game_text(kind: str, internal_id: str | None, english: str, lang: str,
              root: Path | None = None) -> tuple[str, bool]:
    """THE integration point for game-data localization. Returns (text, localized).

    kind:        "base" | "mod" | "essence" | "fossil" | "currency" (documentation only: ids do not overlap)
    internal_id: base item / essence / fossil / currency metadata path, or mod id. None = no id known.
    english:     the English string from the English export (always available).

    English always returns (english, True). Otherwise the string for internal_id from the language's locale file,
    or (english, False) when the file or the entry is missing - the caller must then mark it "EN".
    """
    del kind
    if lang == DEFAULT:
        return english, True
    text = _game_map(lang, root).get(internal_id) if internal_id else None
    if isinstance(text, str) and text:
        return text, True
    return english, False


def game_label(mod_id: str | None, english_label: str, english_text: str | None, lang: str,
               root: Path | None = None) -> tuple[str, bool]:
    """A mod family label ('+# to maximum Life') in `lang`: the same number-to-# step as the English label,
    applied to the localized text of the same mod. Any suffix the English label carries after the mod's own
    generic text (e.g. ' (Veiled)', ' (>= 140)') is kept."""
    from .engine.context import generic_text
    if lang == DEFAULT:
        return english_label, True
    base = generic_text(english_text) if english_text else english_label
    if not english_label.startswith(base):
        return english_label, False
    text, ok = game_text("mod", mod_id, english_text or "", lang, root)
    if not ok:
        return english_label, False
    return generic_text(text) + english_label[len(base):], True


@lru_cache(maxsize=1)
def _named_items(raw_dir: Path | None = None) -> dict:
    """English name -> base item ids of released currency-like items. Price tables carry only names; this turns
    them back into ids so they are localized by id, never by translating the name."""
    from . import config
    path = (raw_dir or config.RAW_DIR) / "base_items.json"
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    seen: dict = {}
    for k, v in items.items():
        if v.get("release_state") == "released" and "Currency" in (v.get("item_class") or "") and v.get("name"):
            seen.setdefault(v["name"], []).append(k)
    return seen


def currency_text(english: str, lang: str, root: Path | None = None) -> tuple[str, bool]:
    """A price-table name in `lang`. When the name belongs to several item ids (e.g. a resonator's socketable and
    stackable versions) it is localized only if every one of those ids has the same localized string."""
    ids = _named_items().get(english, [])
    found = [game_text("currency", i, english, lang, root) for i in ids]
    texts = {t for t, ok in found if ok}
    if found and all(ok for _, ok in found) and len(texts) == 1:
        return texts.pop(), True
    return english, lang == DEFAULT


def mark(text: str, localized: bool) -> str:
    """CLI form: an English fallback carries the "[EN] " prefix."""
    return text if localized else FALLBACK_MARK + text


def _put(obj: dict, field: str, value: tuple[str, bool]) -> None:
    obj[field] = value[0]
    if not value[1]:
        obj.setdefault("_en", []).append(field)


def localize_plan(result: dict, lang: str, root: Path | None = None, targets: bool = True) -> dict:
    """Copy of a planner result with its game-data strings in `lang`: base name, item class, target texts and
    price-table names. Fields that stayed English are listed in each object's "_en". Explanations are rendered by
    poecraft.messages, not here. targets=False: the result was rendered into `lang` with messages.render, whose
    target descriptions already carry localized mod labels - leave them alone."""
    import copy
    if lang == DEFAULT:
        return result
    out = copy.deepcopy(result)
    item = out.get("item", {})
    _put(item, "base", game_text("base", item.get("base_id"), item.get("base", ""), lang, root))
    _put(item, "item_class", (item.get("item_class", ""), False))     # item classes: no locale data yet
    for t in (out.get("targets", []) if targets else []):
        label, ok = game_label(t.get("game_id"), t.get("game_label") or t.get("label", ""), t.get("game_text"),
                               lang, root)
        desc = t.get("description", "")
        if ok and t.get("game_label") and desc.startswith(t["game_label"]):
            _put(t, "description", (label + desc[len(t["game_label"]):], True))
        else:
            _put(t, "description", (desc, False))
    for kp in (out.get("prices") or {}).get("key_prices", []):
        _put(kp, "name", currency_text(kp.get("name", ""), lang, root))
    out["language"] = lang
    return out


def localize_recovery(result: dict, lang: str, root: Path | None = None) -> dict:
    """Recovery output: base name and the mods already on the item, by mod id."""
    import copy
    if lang == DEFAULT:
        return result
    out = copy.deepcopy(result)
    item = out.get("item", {})
    _put(item, "base", game_text("base", item.get("base_id"), item.get("base", ""), lang, root))
    _put(item, "item_class", (item.get("item_class", ""), False))
    for m in out.get("have", []):
        _put(m, "text", game_text("mod", m.get("id"), m.get("text", ""), lang, root))
    out["language"] = lang
    return out
