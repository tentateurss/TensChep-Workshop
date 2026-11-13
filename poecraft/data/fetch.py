"""Step 1 of the data pipeline: download the RePoE JSON export into data/raw/.

The export is regenerated from the game files after every patch, so "updating for a new
league" is just re-running this step followed by build.py.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import urllib.request
from pathlib import Path

from .. import config


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_manifest(source: str, raw_dir: Path) -> dict:
    manifest = {
        "source": source,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": {},
    }
    for name in config.REPOE_FILES:
        path = raw_dir / f"{name}.json"
        manifest["files"][name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    (raw_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def fetch(base_url: str = config.REPOE_BASE_URL, raw_dir: Path = config.RAW_DIR) -> dict:
    """Download every file in config.REPOE_FILES. Uses the minified variants (same content)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    base_url = base_url.rstrip("/") + "/"
    for name in config.REPOE_FILES:
        url = f"{base_url}{name}.min.json"
        dest = raw_dir / f"{name}.json"
        tmp = dest.with_suffix(".part")
        print(f"  downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": config.user_agent()})
        with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        json.loads(tmp.read_text(encoding="utf-8"))  # fail loudly on truncated/invalid downloads
        tmp.replace(dest)
    return _write_manifest(base_url, raw_dir)


def import_dir(src_dir: Path, raw_dir: Path = config.RAW_DIR) -> dict:
    """Offline path: copy manually downloaded RePoE files (name.json or name.min.json)."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name in config.REPOE_FILES:
        for candidate in (src_dir / f"{name}.json", src_dir / f"{name}.min.json"):
            if candidate.exists():
                json.loads(candidate.read_text(encoding="utf-8"))
                shutil.copyfile(candidate, raw_dir / f"{name}.json")
                break
        else:
            raise FileNotFoundError(f"{name}.json (or {name}.min.json) not found in {src_dir}")
    return _write_manifest(f"local:{src_dir.resolve()}", raw_dir)
