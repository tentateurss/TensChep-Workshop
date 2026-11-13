"""Step 2 of the data pipeline: normalise data/raw/*.json into the SQLite database.

Only what the planner needs is kept: craftable equipment bases, prefix/suffix mods
(regular, influenced, essence-only, crafted), essences, fossils, bench crafts and
currency names. List-valued columns are stored as JSON text.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .. import config

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE item_classes(id TEXT PRIMARY KEY, name TEXT, influence_tags TEXT);
CREATE TABLE bases(
    id TEXT PRIMARY KEY, name TEXT, item_class TEXT, drop_level INTEGER,
    tags TEXT, implicits TEXT, properties TEXT, requirements TEXT);
CREATE INDEX bases_class ON bases(item_class);
CREATE INDEX bases_name ON bases(name);
CREATE TABLE mods(
    id TEXT PRIMARY KEY, name TEXT, domain TEXT, generation_type TEXT, type TEXT,
    required_level INTEGER, is_essence_only INTEGER, text TEXT, groups TEXT,
    implicit_tags TEXT, spawn_weights TEXT, generation_weights TEXT, stats TEXT, adds_tags TEXT);
CREATE INDEX mods_domain ON mods(domain, generation_type);
CREATE TABLE essences(
    id TEXT PRIMARY KEY, name TEXT, level INTEGER, tier INTEGER,
    corruption_only INTEGER, item_level_restriction INTEGER);
CREATE TABLE essence_mods(essence_id TEXT, item_class TEXT, mod_id TEXT);
CREATE INDEX essence_mods_class ON essence_mods(item_class);
CREATE TABLE fossils(
    id TEXT PRIMARY KEY, name TEXT, positive_weights TEXT, negative_weights TEXT,
    added_mods TEXT, forced_mods TEXT, allowed_tags TEXT, forbidden_tags TEXT,
    corrupted_essence_chance INTEGER, mirrors INTEGER, rolls_lucky INTEGER);
CREATE TABLE bench_crafts(
    id INTEGER PRIMARY KEY, mod_id TEXT, master TEXT, bench_tier INTEGER,
    item_classes TEXT, cost TEXT);
CREATE TABLE currencies(id TEXT PRIMARY KEY, name TEXT, item_class TEXT);
"""


def _load(raw_dir: Path, name: str):
    path = raw_dir / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing - run `python -m poecraft fetch` first")
    return json.loads(path.read_text(encoding="utf-8"))


def _j(v) -> str:
    return json.dumps(v, separators=(",", ":"))


def build(raw_dir: Path = config.RAW_DIR, db_path: Path = config.DB_PATH) -> dict:
    mods = _load(raw_dir, "mods")
    bases = _load(raw_dir, "base_items")
    essences = _load(raw_dir, "essences")
    fossils = _load(raw_dir, "fossils")
    bench = _load(raw_dir, "crafting_bench_options")
    item_classes = _load(raw_dir, "item_classes")

    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    supported = set(config.SUPPORTED_ITEM_CLASSES)

    con.executemany(
        "INSERT INTO item_classes VALUES (?,?,?)",
        [(k, v.get("name"), _j(v.get("influence_tags", []))) for k, v in item_classes.items()],
    )

    base_rows, cur_rows = [], []
    for bid, b in bases.items():
        if b.get("item_class") in config.CURRENCY_CLASSES and b.get("name"):
            cur_rows.append((bid, b["name"], b["item_class"]))
        if b.get("domain") != "item" or b.get("release_state") != "released":
            continue
        if b.get("item_class") not in supported:
            continue
        base_rows.append((
            bid, b["name"], b["item_class"], b.get("drop_level", 1), _j(b.get("tags", [])),
            _j(b.get("implicits", [])), _j(b.get("properties", {})), _j(b.get("requirements") or {}),
        ))
    con.executemany("INSERT INTO bases VALUES (?,?,?,?,?,?,?,?)", base_rows)
    con.executemany("INSERT INTO currencies VALUES (?,?,?)", cur_rows)

    mod_rows = []
    for mid, m in mods.items():
        if m.get("domain") not in ("item", "crafted"):
            continue
        if m.get("generation_type") not in ("prefix", "suffix"):
            continue
        mod_rows.append((
            mid, m.get("name", ""), m["domain"], m["generation_type"], m.get("type", ""),
            m.get("required_level", 1), int(bool(m.get("is_essence_only"))), m.get("text") or "",
            _j(m.get("groups", [])), _j(m.get("implicit_tags", [])), _j(m.get("spawn_weights", [])),
            _j(m.get("generation_weights", [])), _j(m.get("stats", [])), _j(m.get("adds_tags", [])),
        ))
    con.executemany("INSERT INTO mods VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mod_rows)

    ess_rows, ess_mod_rows = [], []
    for eid, e in essences.items():
        if not e.get("mods"):
            continue
        t = e.get("type", {})
        ess_rows.append((eid, e["name"], e.get("level", 0), t.get("tier", 0),
                         int(bool(t.get("is_corruption_only"))), e.get("item_level_restriction")))
        for cls, mod_id in e["mods"].items():
            ess_mod_rows.append((eid, cls, mod_id))
    con.executemany("INSERT INTO essences VALUES (?,?,?,?,?,?)", ess_rows)
    con.executemany("INSERT INTO essence_mods VALUES (?,?,?)", ess_mod_rows)

    fos_rows = []
    for fid, f in fossils.items():
        if not f.get("name") or "DelveCrafting" not in fid:
            continue
        fos_rows.append((
            fid, f["name"], _j(f.get("positive_mod_weights", [])), _j(f.get("negative_mod_weights", [])),
            _j(f.get("added_mods", [])), _j(f.get("forced_mods", [])), _j(f.get("allowed_tags", [])),
            _j(f.get("forbidden_tags", [])), f.get("corrupted_essence_chance", 0),
            int(bool(f.get("mirrors"))), int(bool(f.get("rolls_lucky"))),
        ))
    con.executemany("INSERT INTO fossils VALUES (?,?,?,?,?,?,?,?,?,?,?)", fos_rows)

    bench_rows = []
    for i, opt in enumerate(bench):
        mod_id = opt.get("actions", {}).get("add_explicit_mod")
        if not mod_id:
            continue
        bench_rows.append((i, mod_id, opt.get("master", ""), opt.get("bench_tier", 0),
                           _j(opt.get("item_classes", [])), _j(opt.get("cost", {}))))
    con.executemany("INSERT INTO bench_crafts VALUES (?,?,?,?,?,?)", bench_rows)

    manifest = {}
    manifest_path = raw_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stats = {
        "bases": len(base_rows), "mods": len(mod_rows), "essences": len(ess_rows),
        "fossils": len(fos_rows), "bench_crafts": len(bench_rows), "currencies": len(cur_rows),
    }
    con.executemany("INSERT INTO meta VALUES (?,?)", [
        ("source", manifest.get("source", "unknown")),
        ("fetched_at", manifest.get("fetched_at", "unknown")),
        ("counts", _j(stats)),
        ("schema_version", "1"),
    ])
    con.commit()
    con.close()
    tmp.replace(db_path)
    return stats
