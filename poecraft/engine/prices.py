"""Currency prices (chaos equivalents).

Lookup order: live poe.ninja snapshot (data/prices_live.json, see data/ninja.py) ->
user-editable config/prices.json -> per-category defaults in config/prices.json.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path

from .. import config
from ..data import ninja

PRICE_LEAGUE = ContextVar("price_league", default=None)

KEY_CURRENCIES = [
    "Chaos Orb", "Orb of Alchemy", "Orb of Transmutation", "Orb of Alteration", "Orb of Augmentation",
    "Regal Orb", "Exalted Orb", "Divine Orb", "Orb of Annulment", "Orb of Scouring",
    "Primitive Chaotic Resonator", "Potent Chaotic Resonator", "Powerful Chaotic Resonator",
    "Prime Chaotic Resonator",
]


class Prices:
    def __init__(self, path: Path = config.PRICES_PATH, live_path: Path = config.LIVE_PRICES_PATH,
                 use_live: bool = True, league: str | None = None):
        self.table = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if use_live:
            self.live_status = ninja.load_status(live_path, league=league or PRICE_LEAGUE.get())
        else:
            self.live_status = {"state": "disabled", "prices": {}, "league": None, "fetched_at": None,
                                "message": "Live prices disabled - using estimates from config/prices.json.",
                                "message_key": "m.price.disabled", "message_params": {}}
        self.live: dict[str, float] = self.live_status.get("prices", {})
        self.missing: set[str] = set()
        self.tier_default_essences: dict[str, float] = {}   # essence name -> per-tier default price used
        self.quoted: set[str] = set()

    def _live(self, name: str) -> float | None:
        v = self.live.get(name)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    def source(self, name: str) -> str:
        if self._live(name) is not None:
            return "live"
        if name in self.tier_default_essences and name in self.missing:
            return "default"
        if name in self.tier_default_essences or name in self.table.get("_estimated", ()):
            return "estimated"      # per-tier essence default, or a config entry marked as a league-start estimate
        return "config" if isinstance(self.table.get(name), (int, float)) else "default"

    def get(self, name: str) -> float:
        self.quoted.add(name)
        live = self._live(name)
        if live is not None:
            return live
        v = self.table.get(name)
        if isinstance(v, (int, float)):
            return float(v)
        if name.endswith("Fossil") and "default_fossil" in self.table:
            self.missing.add(name)
            return float(self.table["default_fossil"])
        self.missing.add(name)
        return float(self.table.get("unknown", 1))

    def essence(self, name: str, level: int) -> float:
        self.quoted.add(name)
        live = self._live(name)
        if live is not None:
            return live
        v = self.table.get(name)
        if isinstance(v, (int, float)):
            return float(v)
        by_level = self.table.get("default_essence_by_level", {})
        if str(level) not in by_level:
            self.missing.add(name)
        price = float(by_level.get(str(level), self.table.get("unknown", 1)))
        self.tier_default_essences[name] = price
        return price

    def basket(self, items: list[tuple[str, float]]) -> float:
        return sum(self.get(n) * c for n, c in items)

    def assessment(self) -> dict:
        """Conservative comparison-wide provenance, including prices of considered alternatives."""
        inputs = [{"name": name, "source": self.source(name)} for name in sorted(self.quoted)
                  if name != "Chaos Orb"]  # chaos is the unit of account, not an exchange-rate estimate
        state = "market"
        if any(x["source"] == "default" for x in inputs):
            state = "unknown"
        elif any(x["source"] != "live" for x in inputs) or (
                inputs and self.live_status.get("state") == "stale"):
            state = "approximate"
        return {"state": state, "scope": "comparison", "inputs": inputs}

    def status_json(self, used_essences=(), used_currencies=()) -> dict:
        """Price status for the UI table; essences a plan uses at a per-tier default are listed as 'estimated',
        and any other currency the plan uses (e.g. Embers/Ichors) is listed with its source."""
        quoted = self.quoted.copy()  # displaying the reference table must not affect cost confidence
        s = {k: v for k, v in self.live_status.items() if k != "prices"}
        s["diagnostics"] = {k: self.live_status.get(k) for k in ("message", "message_key", "message_params")}
        s["live_count"] = len(self.live)
        s["key_prices"] = [{"name": n, "chaos": self.get(n), "source": self.source(n)} for n in KEY_CURRENCIES]
        s["key_prices"] += [{"name": n, "chaos": self.tier_default_essences[n], "source": self.source(n)}
                            for n in sorted(used_essences) if n in self.tier_default_essences]
        s["key_prices"] += [{"name": n, "chaos": self.get(n), "source": self.source(n)}
                            for n in sorted(used_currencies) if n not in KEY_CURRENCIES]
        self.quoted = quoted
        return s
