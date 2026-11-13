"""Game-data localization files: data/locale_game_<code>.json, built from RePoE's per-language exports.

RePoE publishes the same export per game language (https://repoe-fork.github.io/<Language>/...), generated from the
game's own localized files. Only some fields are localized; everything else (ids, stats, spawn weights, levels,
tags ...) must be identical to the English export of the same build. This module

  * downloads <Language>/base_items.min.json and <Language>/mods.min.json into data/raw/<Language>/,
  * checks them against the English data/raw/base_items.json and mods.json: identical id sets and identical
    non-localized fields - any difference REFUSES the language (no partial mapping, the old file is kept),
  * writes data/locale_game_<code>.json: {internal_id: localized string} for base item names (this covers currency,
    essences and fossils, which are base items) and mod texts, plus "_meta".

A localized mod text with fewer lines than the English one is missing a stat line; it is left out, so the
planner shows the English text with the EN mark instead of an incomplete translation. Nothing is ever translated.
"""
from __future__ import annotations

import datetime
import json
import shutil
import urllib.request
from pathlib import Path

from .. import config

FILES = ("base_items", "mods")
LOCALIZED = {"base_items": {"name"}, "mods": {"text", "name"}}
LOCALIZED_PROPERTIES = {"description", "directions", "monster_category", "monster_ability_text"}   # base_items


class LocaleMismatch(ValueError):
    """The localized export does not match the English export of the same build."""


def locale_path(code: str, out_dir: Path = config.DATA_DIR) -> Path:
    return out_dir / f"locale_game_{code}.json"


def fetch_language(language: str, base_url: str = config.REPOE_BASE_URL, raw_dir: Path = config.RAW_DIR) -> Path:
    dest_dir = raw_dir / language
    dest_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/") + "/"
    for name in FILES:
        url = f"{base_url}{urllib.request.quote(language)}/{name}.min.json"
        dest = dest_dir / f"{name}.json"
        tmp = dest.with_suffix(".part")
        print(f"  downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": config.user_agent()})
        with urllib.request.urlopen(req, timeout=180) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        json.loads(tmp.read_text(encoding="utf-8"))      # fail loudly on truncated/invalid downloads
        tmp.replace(dest)
    return dest_dir


def import_language(src_dir: Path, language: str, raw_dir: Path = config.RAW_DIR) -> Path | None:
    """Offline: copy <src_dir>/<Language>/{base_items,mods}[.min].json if present."""
    src = src_dir / language
    if not src.is_dir():
        return None
    dest_dir = raw_dir / language
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        hit = next((p for p in (src / f"{name}.min.json", src / f"{name}.json") if p.exists()), None)
        if hit is None:
            return None
        shutil.copyfile(hit, dest_dir / f"{name}.json")
    return dest_dir


def _strip(kind: str, entry: dict) -> dict:
    """The entry without its localized fields - what must be identical across languages."""
    out = {k: v for k, v in entry.items() if k not in LOCALIZED[kind]}
    if kind == "base_items" and isinstance(out.get("properties"), dict):
        out["properties"] = {k: v for k, v in out["properties"].items() if k not in LOCALIZED_PROPERTIES}
    return out


def compare(kind: str, english: dict, localized: dict, examples: int = 3) -> list[str]:
    """Problems that make the localized export unusable (empty list = identical build)."""
    problems = []
    only_en, only_loc = sorted(set(english) - set(localized)), sorted(set(localized) - set(english))
    if only_en:
        problems.append(f"{kind}: {len(only_en)} ids missing from the localized export, e.g. {only_en[:examples]}")
    if only_loc:
        problems.append(f"{kind}: {len(only_loc)} ids not in the English export, e.g. {only_loc[:examples]}")
    differ = [k for k in sorted(set(english) & set(localized)) if _strip(kind, english[k]) != _strip(kind, localized[k])]
    if differ:
        problems.append(f"{kind}: {len(differ)} ids whose non-localized fields differ from English, e.g. {differ[:examples]}")
    return problems


def build_map(english: dict, localized: dict) -> tuple[dict, list[str]]:
    """(internal_id -> localized string, mod ids left out because a stat line is missing)."""
    out, dropped = {}, []
    for k, v in english["base_items"].items():
        name = localized["base_items"][k].get("name")
        if v.get("name") and name:
            out[k] = name
    for k, v in english["mods"].items():
        text = localized["mods"][k].get("text")
        if not (v.get("text") and text):
            continue
        if text.count("\n") < v["text"].count("\n"):
            dropped.append(k)
            continue
        out[k] = text
    return out, dropped


def build(code: str, language: str, raw_dir: Path = config.RAW_DIR, out_dir: Path = config.DATA_DIR) -> dict:
    """Validate raw/<Language>/ against the English raw files and write locale_game_<code>.json.
    Raises LocaleMismatch (writing nothing) if the exports are not the same build."""
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))
    english = {k: load(raw_dir / f"{k}.json") for k in FILES}
    localized = {k: load(raw_dir / language / f"{k}.json") for k in FILES}
    problems = [p for k in FILES for p in compare(k, english[k], localized[k])]
    if problems:
        raise LocaleMismatch(f"{language} export refused, not the same build as the English export: "
                             + "; ".join(problems))
    mapping, dropped = build_map(english, localized)
    if set(mapping) & {"_meta"}:
        raise LocaleMismatch("an internal id collides with '_meta'")
    manifest = {}
    try:
        manifest = load(raw_dir / "manifest.json")
    except (OSError, ValueError):
        pass
    meta = {
        "language": language,
        "source": f"{manifest.get('source', config.REPOE_BASE_URL)}{language}/ (base_items, mods)",
        "same_build_as": manifest.get("fetched_at"),
        "extracted_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys": "base item / essence / fossil / currency metadata path -> name; mod id -> mod text",
        "base_items": sum(1 for k in mapping if k in english["base_items"]),
        "mods": sum(1 for k in mapping if k in english["mods"]),
        "excluded_incomplete_mod_texts": dropped,
        "note": "Joined by internal id only; nothing translated. Missing ids fall back to English with the EN mark.",
    }
    path = locale_path(code, out_dir)
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps({"_meta": meta, **mapping}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
    return meta


def update_all(base_url: str | None = config.REPOE_BASE_URL, from_dir: Path | None = None,
               raw_dir: Path = config.RAW_DIR, out_dir: Path = config.DATA_DIR) -> dict:
    """Fetch (or import) and build every language in config.LOCALE_LANGUAGES. Returns {code: meta | error text};
    a refused or failed language keeps its previous file."""
    results = {}
    for code, language in config.LOCALE_LANGUAGES.items():
        try:
            if from_dir is not None:
                if import_language(from_dir, language, raw_dir) is None:
                    results[code] = f"skipped: {from_dir / language} has no base_items/mods files"
                    continue
            elif base_url is not None:
                fetch_language(language, base_url, raw_dir)
            results[code] = build(code, language, raw_dir, out_dir)
        except LocaleMismatch as e:
            results[code] = f"REFUSED: {e}"
        except (OSError, ValueError) as e:
            results[code] = f"failed: {e}"
    return results
