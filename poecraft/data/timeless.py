"""Timeless Jewel data: what a seed does to each passive in a jewel's radius.

Sources (downloaded by `python -m poecraft timeless update`, also part of `update`, into data/timeless/):
  * Path of Building Community (MIT License, see data/timeless/NOTICE.txt): the precomputed look-up tables
    src/Data/TimelessJewelData/<Jewel>.zip(.partN) - for every (passive, seed) the replacement / addition id (and the
    Glorious Vanity rolls) -, NodeIndexMapping.lua (passive id -> row of those tables) and LegionPassives.lua (the
    texts of the replacement passives and additions);
  * Grinding Gear Games' passive tree export (github.com/grindinggear/skilltree-export, data.json): node names,
    stats and positions, the jewel sockets;
  * RePoE's stat descriptions in English / Russian / German: the game's own wording of the jewel's seed line
    ("Commanded leadership over {1} warriors under Kaom"), for reading pasted jewels in any of those languages.

Nothing is calculated from the game's random number generator here: the tables are read, never generated. The
Lua files are converted to JSON once, at download; the tables are stored inflated, so a look-up is one file seek.
"""
from __future__ import annotations

import json
import hashlib
import uuid
import re
import shutil
import time
import urllib.request
import zlib
from functools import lru_cache
from pathlib import Path

from .. import config

POB_RAW = "https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/dev/"
POB_DIR = POB_RAW + "src/Data/TimelessJewelData/"
POB_LICENSE = POB_RAW + "LICENSE.md"
TREE_URL = "https://raw.githubusercontent.com/grindinggear/skilltree-export/master/data.json"
STAT_TRANSLATIONS = {"en": "stat_translations.min.json", "ru": "Russian/stat_translations.min.json",
                     "de": "German/stat_translations.min.json"}
SEED_STAT = "local_unique_jewel_alternate_tree_seed"

TIMELESS_DIR = config.DATA_DIR / "timeless"
# jewel types as numbered by the game (local_unique_jewel_alternate_tree_version) and by PoB's tables
JEWELS = {1: "Glorious Vanity", 2: "Lethal Pride", 3: "Brutal Restraint", 4: "Militant Faith", 5: "Elegant Hubris",
          6: "Heroic Tragedy"}
CONQUEROR_TYPE = {1: "vaal", 2: "karui", 3: "maraketh", 4: "templar", 5: "eternal", 6: "kalguur"}
# seed range of each table; Elegant Hubris seeds are multiples of 20, its table is indexed by seed / 20
SEEDS = {1: (100, 8000), 2: (10000, 18000), 3: (500, 8000), 4: (2000, 10000), 5: (100, 8000), 6: (100, 8000)}
SEED_DIVISOR = {5: 20}
JEWEL_RADIUS = 1800                  # "Radius: Large" since the 3.16 tree
ADDITIONS = 337                      # ids below this are additions, from it on replacement passives (PoB)
# 3.17+ tree: orbits with 16 and 40 positions are not evenly spaced (GGG's export README)
ORBIT_ANGLES = {16: [0, 30, 45, 60, 90, 120, 135, 150, 180, 210, 225, 240, 270, 300, 315, 330],
                40: [0, 10, 20, 30, 40, 45, 50, 60, 70, 80, 90, 100, 110, 120, 130, 135, 140, 150, 160, 170, 180,
                     190, 200, 210, 220, 225, 230, 240, 250, 260, 270, 280, 290, 300, 310, 315, 320, 330, 340, 350]}


class TimelessDataMissing(RuntimeError):
    pass


# ---------------------------------------------------------------- download

_downloads = {}


def _get(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": config.user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        _downloads[url] = hashlib.sha256(raw).hexdigest()
        return raw


def _download_lut(jewel: str) -> bytes:
    """The compressed table: one .zip, or .zip.part0, .part1 ... (GitHub's file size limit) joined."""
    name = jewel.replace(" ", "")
    try:
        return _get(POB_DIR + name + ".zip")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    parts, i = [], 0
    while True:
        try:
            parts.append(_get(POB_DIR + f"{name}.zip.part{i}"))
        except urllib.error.HTTPError as e:
            if e.code == 404 and parts:
                return b"".join(parts)
            raise
        i += 1


def _update(out_dir: Path | None = None, log=print) -> dict:
    """Download and convert everything into data/timeless/ (about 35 MB to download, 70 MB on disk)."""
    out_dir = out_dir or TIMELESS_DIR
    tmp = out_dir.with_name(out_dir.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    log("  Path of Building: node index mapping and passive texts")
    index = convert_index(_get(POB_DIR + "NodeIndexMapping.lua").decode("utf-8"))
    (tmp / "index.json").write_text(json.dumps(index), encoding="utf-8")
    legion = convert_legion(_get(POB_DIR + "LegionPassives.lua").decode("utf-8"))
    (tmp / "legion.json").write_text(json.dumps(legion), encoding="utf-8")
    sizes = {}
    for jt, jewel in JEWELS.items():
        log(f"  Path of Building: {jewel} look-up table")
        raw = zlib.decompress(_download_lut(jewel))
        lo, hi = SEEDS[jt]
        if jt != 1 and len(raw) < index["notables"] * (hi - lo + 1):
            raise ValueError(f"{jewel}: table too short ({len(raw)} bytes) for the node index mapping")
        (tmp / f"lut_{jt}.bin").write_bytes(raw)
        sizes[jewel] = len(raw)
    (tmp / "NOTICE.txt").write_text(
        "The files lut_*.bin, index.json and legion.json are converted from Path of Building Community\n"
        "(https://github.com/PathOfBuildingCommunity/PathOfBuilding, src/Data/TimelessJewelData), used under the\n"
        "license below. Item data (c) Grinding Gear Games.\n\n" + _get(POB_LICENSE).decode("utf-8"), encoding="utf-8")
    log("  Grinding Gear Games: passive tree export")
    tree = convert_tree(json.loads(_get(TREE_URL)))
    (tmp / "tree.json").write_text(json.dumps(tree), encoding="utf-8")
    log("  RePoE: the seed line in English / Russian / German")
    base = config.REPOE_BASE_URL.rstrip("/") + "/"
    conq = {lang: seed_templates(json.loads(_get(base + path)), lang) for lang, path in STAT_TRANSLATIONS.items()}
    (tmp / "conquerors.json").write_text(json.dumps(conq, ensure_ascii=False), encoding="utf-8")
    manifest = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "sources": {"pob": POB_DIR, "tree": TREE_URL, "stat_translations": base}, "tables": sizes,
                "tree_nodes": len(tree["nodes"]), "sockets": len(tree["sockets"])}
    (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest['revisions'] = dict(_revisions)
    manifest['game_version'] = None  # a repository revision alone proves no PoE version
    manifest['tree_game_version'] = None
    manifest['downloads'] = dict(_downloads)
    manifest['sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp.iterdir() if p.is_file() and p.name != 'manifest.json'}
    (tmp / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    backup = out_dir.with_name(out_dir.name + '.backup-' + uuid.uuid4().hex)
    if out_dir.exists():
        out_dir.rename(backup)
    try:
        tmp.replace(out_dir)
    except BaseException:
        if backup.exists(): backup.rename(out_dir)
        raise
    _clear()
    return manifest


# ---------------------------------------------------------------- conversion

_TOKEN = re.compile(r'\s*(?:--[^\n]*\n\s*)*(\{|\}|\[|\]|=|,|"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?'
                    r'|true|false|nil|return|[A-Za-z_]\w*)')


def lua_table(text: str):
    """A Lua data file ("return { ... }" of literals) as Python: tables with keys 1..n become lists."""
    toks = _TOKEN.findall(text)
    pos = 0

    def value():
        nonlocal pos
        t = toks[pos]
        pos += 1
        if t == "{":
            arr, obj = [], {}
            while toks[pos] != "}":
                if toks[pos] == "[":
                    pos += 1
                    k = value()
                    pos += 2                       # "]" "="
                    obj[k] = value()
                else:
                    arr.append(value())
                if toks[pos] == ",":
                    pos += 1
            pos += 1
            if obj and all(isinstance(k, int) for k in obj) and sorted(obj) == list(range(1, len(obj) + 1)):
                return [obj[i] for i in range(1, len(obj) + 1)]
            return obj or arr
        if t.startswith('"'):
            return t[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape").encode("latin-1").decode("utf-8") \
                if "\\" in t else t[1:-1]
        if t in ("true", "false"):
            return t == "true"
        if t == "nil":
            return None
        return float(t) if "." in t or "e" in t.lower() else int(t)

    while toks[pos] == "return":
        pos += 1
    return value()


def convert_index(text: str) -> dict:
    """NodeIndexMapping.lua: nodeIDList[<passive id>] = { index = i, size = n } lines, the sizes, and the per-jewel
    local -> global id tables."""
    nodes = {m.group(1): [int(m.group(2)), int(m.group(3))]
             for m in re.finditer(r"nodeIDList\[(\d+)\] = \{ index = (\d+), size = (\d+) \}", text)}
    l2g: dict = {}
    for m in re.finditer(r'nodeIDList\["localIdToGlobalId"\]\[(\d+)\]\[(\d+)\] = (\d+)', text):
        l2g.setdefault(m.group(1), {})[m.group(2)] = int(m.group(3))
    size = int(re.search(r'nodeIDList\["size"\] = (\d+)', text).group(1))
    notables = int(re.search(r'nodeIDList\["sizeNotable"\] = (\d+)', text).group(1))
    return {"size": size, "notables": notables, "nodes": nodes, "local_to_global": l2g}


def convert_legion(text: str) -> dict:
    data = lua_table(text)
    keep = lambda n: {"id": n["id"], "name": n["dn"], "sd": list(n.get("sd") or []),
                      "stats": {k: {f: v[f] for f in ("fmt", "index", "min", "max") if f in v}
                                for k, v in (n.get("stats") or {}).items()},
                      "sorted": list(n.get("sortedStats") or [])}
    return {"nodes": [keep(n) for n in data["nodes"]], "additions": [keep(n) for n in data["additions"]]}


def node_position(group: dict, orbit: int, index: int, radii, per_orbit) -> tuple[float, float]:
    import math
    n = per_orbit[orbit]
    angle = math.radians(ORBIT_ANGLES[n][index] if n in ORBIT_ANGLES else 360 * index / n)
    r = radii[orbit]
    return group["x"] + r * math.sin(angle), group["y"] - r * math.cos(angle)


def convert_tree(tree: dict) -> dict:
    """The main passive tree only (no ascendancies, cluster jewel proxies or charm sockets): every node a Timeless
    Jewel can change, the sockets it can go in, and where the classes start (for naming the sockets)."""
    radii, per_orbit = tree["constants"]["orbitRadii"], tree["constants"]["skillsPerOrbit"]
    groups = tree["groups"]
    nodes, sockets, starts = {}, [], []
    for nid, n in tree["nodes"].items():
        if str(n.get("group")) not in groups or n.get("ascendancyName") or n.get("isProxy") \
                or groups[str(n["group"])].get("isProxy") \
                or n.get("isBlighted") or n.get("isAscendancyStart"):
            continue
        x, y = node_position(groups[str(n["group"])], n.get("orbit", 0), n.get("orbitIndex", 0), radii, per_orbit)
        if "classStartIndex" in n:
            starts.append({"index": n["classStartIndex"], "x": round(x), "y": round(y)})
            continue
        kind = ("socket" if n.get("isJewelSocket") else "mastery" if n.get("isMastery") else "keystone"
                if n.get("isKeystone") else "notable" if n.get("isNotable") else "normal")
        if kind == "socket" and ("parent" in (n.get("expansionJewel") or {}) or "Charm" in n.get("name", "")):
            continue
        nodes[nid] = {"name": n.get("name", ""), "kind": kind, "x": round(x), "y": round(y),
                      "stats": list(n.get("stats") or [])}
        if kind == "socket":
            sockets.append(nid)
    classes = {c_i: c["name"] for c_i, c in enumerate(tree["classes"])}
    for s in starts:
        s["class"] = classes.get(s["index"], "")
    return {"nodes": nodes, "sockets": sockets, "starts": starts, "radius": JEWEL_RADIUS}


def seed_templates(translations: list, lang: str) -> list[dict]:
    """The game's seed lines in one language: [{"version", "keystone", "revision" (0/1), "line" (with {1})}]."""
    for e in translations:
        if SEED_STAT in e.get("ids", []):
            key = next(k for k in e if k not in ("ids", "hidden", "trade_stats"))
            out = []
            for v in e[key]:
                cond = v.get("condition") or []
                if len(cond) < 3 or "min" not in cond[0] or "min" not in cond[2]:
                    continue
                rev = cond[3].get("min", 0) if len(cond) > 3 else 0
                out.append({"version": cond[0]["min"], "keystone": cond[2]["min"], "revision": 1 if rev else 0,
                            "line": v["string"].split("\n")[0]})
            return out
    raise ValueError(f"{lang}: no '{SEED_STAT}' entry in the stat descriptions")


# ---------------------------------------------------------------- loading

def available() -> bool:
    from .timeless_status import status
    return status()["compatibility"] not in ("missing", "incompatible")


def _load(name: str):
    path = TIMELESS_DIR / name
    if not path.exists():
        raise TimelessDataMissing(name)
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def tree() -> dict:
    return _load("tree.json")


@lru_cache(maxsize=1)
def legion() -> dict:
    return _load("legion.json")


@lru_cache(maxsize=1)
def index() -> dict:
    return _load("index.json")


@lru_cache(maxsize=1)
def conquerors() -> dict:
    return _load("conquerors.json")


@lru_cache(maxsize=1)
def _gv_offsets() -> list[int]:
    """Glorious Vanity: where each passive's block starts (after the sizes part: one length byte per passive/seed)."""
    idx = index()
    lo, hi = SEEDS[1]
    pos = idx["size"] * (hi - lo + 1)
    by_row = sorted(idx["nodes"].values())
    out = [0] * (max(r for r, _ in by_row) + 1)
    for row, size in by_row:
        out[row] = pos
        pos += size
    return out


def _clear():
    for f in (tree, legion, index, conquerors, _gv_offsets, column):
        f.cache_clear()


def _global(jt: int, local: int) -> int:
    return index()["local_to_global"].get(str(jt), {}).get(str(local), local)


@lru_cache(maxsize=512)
def column(jt: int, node_id: str) -> tuple | None:
    """Every seed's table entry for one passive, in seed order (index 0 = the lowest seed; Elegant Hubris: seed / 20):
    a tuple of tuples, the same entries read() returns. One file read per passive - the whole seed range at once."""
    idx = index()
    row = idx["nodes"].get(str(node_id))
    if row is None:
        return None
    row = row[0]
    lo, hi = SEEDS[jt]
    width = hi - lo + 1
    path = TIMELESS_DIR / f"lut_{jt}.bin"
    if not path.exists():
        raise TimelessDataMissing(path.name)
    with open(path, "rb") as f:
        if jt != 1:
            if row >= idx["notables"]:
                return None
            f.seek(row * width)
            return tuple((_global(jt, b),) for b in f.read(width))
        f.seek(row * width)
        lengths = f.read(width)
        f.seek(_gv_offsets()[row])
        block = f.read(sum(lengths))
    out, pos = [], 0
    for n in lengths:
        entry = list(block[pos:pos + n])
        pos += n
        if n in (2, 3):
            entry[0] = _global(jt, entry[0])
        elif n in (6, 8):
            entry[:n // 2] = [_global(jt, v) for v in entry[:n // 2]]
        out.append(tuple(entry))
    return tuple(out)


def read(jt: int, seed: int, node_id: str) -> list[int] | None:
    """The table entry for one passive: [id] (replacement / addition) - Glorious Vanity: [id, roll(s)] or 3 / 4
    addition ids followed by their rolls. None: the passive is not in the tables (or the seed is out of range)."""
    idx = index()
    row = idx["nodes"].get(str(node_id))
    lo, hi = SEEDS[jt]
    seed //= SEED_DIVISOR.get(jt, 1)
    if row is None or not lo <= seed <= hi:
        return None
    row = row[0]
    off, width = seed - lo, hi - lo + 1
    path = TIMELESS_DIR / f"lut_{jt}.bin"
    if not path.exists():
        raise TimelessDataMissing(path.name)
    with open(path, "rb") as f:
        if jt != 1:
            if row >= idx["notables"]:
                return None
            f.seek(row * width + off)
            return [_global(jt, f.read(1)[0])]
        f.seek(row * width)
        lengths = f.read(off + 1)
        f.seek(_gv_offsets()[row] + sum(lengths[:off]))
        entry = list(f.read(lengths[off]))
    if len(entry) in (2, 3):
        entry[0] = _global(jt, entry[0])
    elif len(entry) in (6, 8):
        half = len(entry) // 2
        entry[:half] = [_global(jt, v) for v in entry[:half]]
    return entry


_revisions = {}


def _revision(repository, ref):
    try:
        obj = json.loads(_get(f'https://api.github.com/repos/{repository}/commits/{ref}'))
        revision = obj.get('sha', '')
        return revision if re.fullmatch('[a-f0-9]{40}', revision) else None
    except (OSError, ValueError):
        return None


def update(out_dir=None, log=print):
    global POB_DIR, POB_LICENSE, TREE_URL, _revisions
    original = POB_DIR, POB_LICENSE, TREE_URL
    _downloads.clear()
    pob = _revision('PathOfBuildingCommunity/PathOfBuilding', 'dev')
    tree_revision = _revision('grindinggear/skilltree-export', 'master')
    _revisions = {'pob': pob, 'tree': tree_revision}
    try:
        if pob:
            base = f'https://raw.githubusercontent.com/PathOfBuildingCommunity/PathOfBuilding/{pob}/'
            POB_DIR = base + 'src/Data/TimelessJewelData/'
            POB_LICENSE = base + 'LICENSE.md'
        if tree_revision:
            TREE_URL = f'https://raw.githubusercontent.com/grindinggear/skilltree-export/{tree_revision}/data.json'
        return _update(out_dir, log)
    finally:
        POB_DIR, POB_LICENSE, TREE_URL = original
