"""Crafting templates for the CLI: named sets of base, item level, influences, targets (with thresholds) and objective.

Stored as JSON in data/templates.json (or the file named by POECRAFT_TEMPLATES). The web UI keeps its own templates
in the browser (localStorage) - the two are separate stores. A template stores the base by internal id and name, and
targets as the planner's target specs (stat line signature, threshold, flags), so it does not depend on the language.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import config


def path() -> Path:
    return Path(os.environ.get("POECRAFT_TEMPLATES") or config.DATA_DIR / "templates.json")


def load_all() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get(name: str) -> dict | None:
    return load_all().get(name)


def save(name: str, template: dict) -> Path:
    data = load_all()
    data[name] = dict(template, saved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)                      # never leave a half-written file behind
    return p


def delete(name: str) -> bool:
    data = load_all()
    if name not in data:
        return False
    del data[name]
    path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return True
