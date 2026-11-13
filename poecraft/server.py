"""Local web UI: a stdlib HTTP server exposing a small JSON API plus the static front-end."""
from __future__ import annotations

import json
import re
import mimetypes
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, config, i18n, ocr
from .ratelimit import RateLimiter
from .data import ninja
from .data.eldritch import get_eldritch
from .data.elevated import get_elevated
from .data.special import get_special
from .data.veiled import get_veiled
from .data.repository import get_data, get_rules
from .engine import eldritch, influence, special, veiled
from .engine.context import ItemContext, generic_text, stat_scale, to_display
from .engine.itemtext import parse_item_text
from .engine.modsearch import mod_search
from .engine.planner import Infeasible, Planner, feature_rows, rule_note
from .messages import M, Text, missing_keys, render
from .engine.prices import Prices

_plan_lock = threading.Lock()
MAX_SIMS = int(config.os.environ.get("POECRAFT_MAX_SIMS", "20000" if config.PRODUCTION else "200000"))

# per-IP limits by endpoint class (production); the planner itself runs one plan at a time
_LIMITS = {"plan": RateLimiter(*config.RATE_PLAN), "trade": RateLimiter(*config.RATE_TRADE),
           "evaluate": RateLimiter(*config.RATE_EVAL),
           "ocr": RateLimiter(*config.RATE_OCR), "api": RateLimiter(*config.RATE_API)}
_LIMITED = {"/api/plan": "plan", "/api/recover": "plan", "/api/trade": "trade", "/api/ocr": "ocr",
            "/api/evaluate": "evaluate"}
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # the UI is one script file and inline style attributes; it only talks to this server
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
}


PAGES = {"/": "hub.html", "": "hub.html", "/hub": "hub.html", "/craft": "index.html", "/evaluator": "evaluator.html"}


class Busy(Exception):
    """The planner is busy with other requests for longer than POECRAFT_PLAN_QUEUE_WAIT."""


# Computed plans / recoveries, language-free, so a language switch re-renders without re-planning (most recent 16)
_RESULTS: dict = {}
_RESULTS_MAX = 16


def _keep(raw: dict) -> str:
    import uuid
    rid = uuid.uuid4().hex
    _RESULTS[rid] = raw
    while len(_RESULTS) > _RESULTS_MAX:
        _RESULTS.pop(next(iter(_RESULTS)))
    return rid


def _present(raw: dict, lang: str, rid: str) -> dict:
    """A language-free result in `lang`: explanations rendered from the message keys, game data by internal id."""
    out = render(raw, lang)
    out = (i18n.localize_recovery(out, lang) if "have" in out else i18n.localize_plan(out, lang, targets=False))
    if "prices" in out:
        out["prices"] = _localized_prices(out["prices"], lang)
    return dict(out, result_id=rid, language=lang, untranslated_keys=missing_keys(raw, lang))


def _error(e: Exception, lang: str) -> str:
    """An error message in `lang` when it carries a message object (otherwise as it is)."""
    msg = e.args[0] if e.args else ""
    return msg.render(lang) if isinstance(msg, Text) else str(e)


def _localized_prices(status: dict, lang: str) -> dict:
    """Price-table names (currency items) in `lang`, by item id; anything else stays English, marked. The status
    message is rendered from its message key."""
    if status.get("message_key"):
        status["message"] = M(status["message_key"], **status.get("message_params", {})).render(lang)
    for kp in status.get("key_prices", []):
        kp["name"], ok = i18n.currency_text(kp["name"], lang)
        if not ok:
            kp.setdefault("_en", []).append("name")
    return status


# Currency names that UI strings mention ({fracture}, {divine} ...): filled in from the game's own localization by
# item id (i18n.currency_text), never translated by this project. Missing ones stay English and are marked.
UI_GAME_NAMES = {"fracture": "Fracturing Orb", "conflict": "Orb of Conflict", "dominance": "Orb of Dominance",
                 "veiled_chaos": "Veiled Chaos Orb", "alteration": "Orb of Alteration", "regal": "Regal Orb",
                 "exalt": "Exalted Orb", "divine": "Divine Orb"}


def _game_names(lang: str) -> dict:
    return {k: list(i18n.currency_text(v, lang)) for k, v in UI_GAME_NAMES.items()}


def _localize_families(fams: list[dict], lang: str) -> None:
    """Game-data strings of the mod list in `lang`, by id: family labels (number-to-# step on the localized text
    of the mod the English label came from), tier texts (mod id) and essence / fossil tier labels (item id).
    Anything without a localized string stays English and is listed in the object's "_en"."""
    if lang == i18n.DEFAULT:
        return
    for f in fams:
        f["label_en"] = f["label"]               # the mod search matches English words in any language too
        src = next((t for t in f.get("tiers", []) if t.get("text") and f["label"].startswith(generic_text(t["text"]))),
                   None)
        label, ok = (i18n.game_label(src["mod_id"], f["label"], src["text"], lang) if src else (f["label"], False))
        f["label"] = label
        if not ok:
            f.setdefault("_en", []).append("label")
        for t in f.get("tiers", []):
            t["text"], ok = i18n.game_text("mod", t.get("mod_id"), t.get("text") or "", lang)
            if not ok:
                t.setdefault("_en", []).append("text")
            if t.get("label_id"):
                t["label"], ok = i18n.game_text("base", t["label_id"], t["label"], lang)
            else:
                ok = bool(re.fullmatch(r"T\d+", t.get("label") or ""))      # "T1" is UI text, not game data
            if not ok:
                t.setdefault("_en", []).append("label")


def _imported(result: dict, lang: str) -> dict:
    """An item import in `lang`: messages rendered, the base name as the game writes it in that language."""
    out = render(result, lang)
    if out.get("base_id"):
        out["base_name_local"] = i18n.game_text("base", out["base_id"], out["base_name"], lang)[0]
    return out


_FAMILY_CACHE: dict = {}       # (base, ilvl, influences, lang) -> _families output, for the mod search


def _families_cached(q: dict) -> dict:
    key = tuple(q.get(k, [""])[0] for k in ("base", "ilvl", "inf", "lang"))
    if key not in _FAMILY_CACHE:
        if len(_FAMILY_CACHE) >= 8:
            _FAMILY_CACHE.pop(next(iter(_FAMILY_CACHE)))
        _FAMILY_CACHE[key] = _families(q)
    return _FAMILY_CACHE[key]


def _display_units(fams: list[dict]) -> None:
    """Tier ranges and max/min values of the mod list in the units the game displays (the UI shows them and takes
    thresholds in them; the planner converts back - see context.stat_scale)."""
    for f in fams:
        if stat_scale(f.get("primary_stat")) is None:
            continue
        sid = f["primary_stat"]
        for t in f.get("tiers", []):
            t["min"], t["max"] = to_display(sid, t.get("min")), to_display(sid, t.get("max"))
        f["max_value"], f["min_value"] = to_display(sid, f.get("max_value")), to_display(sid, f.get("min_value"))


def _families(q: dict) -> dict:
    data = get_data()
    base = data.bases[q["base"][0]]
    inf = [x for x in q.get("inf", [""])[0].split(",") if x]
    ctx = ItemContext(data, get_rules(), base, int(q.get("ilvl", ["86"])[0]), inf)
    fams = sorted(ctx.families().values(), key=lambda f: (f.affix, "roll" not in f.sources, f.label))
    out = [dict(f.to_json(), key=f.signature) for f in fams]
    # each influence's Exalt pool, when the base has no influence yet (Shaper/Elder/Conqueror Exalted Orbs)
    if not ctx.influences:
        for inf in influence.INFLUENCES:
            c = influence.influence_context(get_data(), get_rules(), base, ctx.ilvl, (inf,))
            sigs = {e.mod.signature for e in influence.exalt_pool(c)}
            for f in sorted(c.families().values(), key=lambda f: (f.affix, f.label)):
                if f.signature in sigs:
                    j = f.to_json()
                    j.update(key=f"{f.signature}@{inf}", influence_target=inf, sources=["influence"],
                             label=f"{f.label} ({inf.title()})",
                             tiers=[t for t in j["tiers"] if t["source"] == "roll"])
                    out.append(j)
    # Eldritch implicits this base can take (Embers: Searing Exarch, Ichors: Eater of Worlds); none if influenced
    if not ctx.influences:
        tier_names = {v: k for k, v in get_rules()["eldritch_tier_names"].items()}
        for f in sorted(eldritch.base_families(get_eldritch(), base.item_class, ctx.tags).values(),
                        key=lambda f: (f.side, f.label)):
            tiers = [{"mod_id": m.id, "source": "eldritch", "label": f"T{t} ({tier_names.get(t, '?')})",
                      "text": m.text, "ilvl": m.required_level, "weight": 0, "min": m.stats[0].min,
                      "max": m.stats[0].max} for t, m in sorted(f.tiers.items())]
            out.append({"signature": f.key, "key": f.key, "affix": f.side, "label": f.label,
                        "primary_stat": f.primary_stat,
                        "influence": False, "sources": ["eldritch"], "tiers": tiers, "eldritch": f.side,
                        "max_value": max(t["max"] for t in tiers), "min_value": min(t["min"] for t in tiers),
                        "roll_weight": 0})
    # Elevated modifiers for this base's influences (Orb of Dominance, range plan; armour slots only)
    edata = get_elevated()
    if base.item_class in edata.classes:
        tags = set(ctx.tags)
        types = {m.type for m in get_data().mods.values() if m.spawn_weight(tags) > 0}
        seen = set()
        for e in sorted(edata.mods, key=lambda e: (e.mod.affix, e.mod.text)):
            if e.influence not in ctx.influences or e.base_type not in types or (e.influence, e.mod.signature) in seen:
                continue
            seen.add((e.influence, e.mod.signature))
            st = e.mod.stats[0]
            out.append({"signature": e.mod.signature, "key": f"elevated|{e.influence}|{e.mod.signature}",
                        "affix": e.mod.affix, "label": f"{generic_text(e.mod.text)} (Elevated {e.influence.title()})",
                        "primary_stat": st.id, "influence": False, "sources": ["elevated"],
                        "tiers": [{"mod_id": e.mod.id, "source": "elevated", "label": e.mod.name, "text": e.mod.text,
                                   "ilvl": e.mod.required_level, "weight": 0, "min": st.min, "max": st.max}],
                        "elevated": e.influence, "max_value": st.max, "min_value": st.min, "roll_weight": 0})
    # Special-fossil modifiers (supported fossils only) and Beastcraft outcomes (listed so the reason shows)
    sdata, rules = get_special(), get_rules()
    tags = set(ctx.tags)
    for name, f in sdata.fossils.items():
        if special.verdict(f, rules)[0] != special.SUPPORTED:
            continue
        if (f.allowed_tags and not set(f.allowed_tags) & tags) or set(f.forbidden_tags) & tags:
            continue
        for m in f.added + f.forced:
            if m not in f.forced and (m.spawn_weight(tags) <= 0 or m.required_level > ctx.ilvl):
                continue
            st = m.stats[0]
            out.append({"signature": m.signature, "key": f"fossil|{name}|{m.signature}", "affix": m.affix,
                        "label": f"{generic_text(m.text)} ({name.split()[0]})", "primary_stat": st.id,
                        "influence": False, "sources": ["fossil"], "special_fossil": name,
                        "tiers": [{"mod_id": m.id, "source": "fossil", "label": name, "text": m.text,
                                   "label_id": next((x.id for x in get_data().fossils if x.name == name), None),
                                   "ilvl": m.required_level, "weight": m.spawn_weight(tags), "min": st.min,
                                   "max": st.max}],
                        "max_value": st.max, "min_value": st.min, "roll_weight": 0})
    for m in sdata.beast_outcomes:
        st = m.stats[0]
        out.append({"signature": m.signature, "key": f"beastcraft|{m.id}", "affix": m.affix,
                    "label": f"{generic_text(m.text)} (Beastcraft)", "primary_stat": st.id, "influence": False,
                    "sources": ["beastcraft"], "beastcraft": True,
                    "not_supported": render(rule_note("beastcraft_missing"), q.get("lang", [i18n.DEFAULT])[0]),
                    "tiers": [{"mod_id": m.id, "source": "beastcraft", "label": m.name, "text": m.text,
                               "ilvl": m.required_level, "weight": 0, "min": st.min, "max": st.max}],
                    "max_value": st.max, "min_value": st.min, "roll_weight": 0})
    # Veiled outcomes this base can unveil into (Aisling / Veiled Chaos Orb, then unveil; generic pool only)
    vdata = get_veiled()
    vfams = veiled.all_families(vdata)
    by_sig: dict = {}
    for side in ("prefix", "suffix"):
        for m, w in veiled.unveil_pool(vdata, ctx, side):
            by_sig.setdefault(m.signature, []).append((m, w))
    for sig, mods in sorted(by_sig.items(), key=lambda x: (vfams[x[0]].affix, vfams[x[0]].label)):
        f = vfams[sig]
        tiers = [{"mod_id": m.id, "source": "veiled", "label": m.name, "text": m.text, "ilvl": m.required_level,
                  "weight": w, "min": m.stats[0].min, "max": m.stats[0].max} for m, w in mods]
        out.append({"signature": sig, "key": "veiled|" + sig, "affix": f.affix, "label": f"{f.label} (Veiled)",
                    "primary_stat": f.primary_stat, "influence": False, "sources": ["veiled"], "tiers": tiers,
                    "veiled": True, "max_value": max(t["max"] for t in tiers),
                    "min_value": min(t["min"] for t in tiers), "roll_weight": 0})
    lang = q.get("lang", [i18n.DEFAULT])[0]          # game data goes through the single integration point
    _display_units(out)
    _localize_families(out, lang)
    name, ok = i18n.game_text("base", base.id, base.name, lang)
    return {"base": {"id": base.id, "name": name, "item_class": base.item_class,
                     "_en": ([] if ok else ["name"]) + ([] if lang == i18n.DEFAULT else ["item_class"]),
                     "tags": sorted(ctx.tags), "properties": base.properties},
            "families": out}


class Handler(BaseHTTPRequestHandler):
    server_version = "poecraft"
    sys_version = ""
    timeout = config.SOCKET_TIMEOUT          # a client that stalls a connection is dropped
    production = config.PRODUCTION

    def log_message(self, fmt, *args):       # local: quiet; production: one access line per request on stdout
        if self.production:
            print(f"{self._client_ip()} - {fmt % args}", flush=True)

    def _client_ip(self) -> str:
        if config.TRUST_PROXY:                # behind the reverse proxy: its X-Forwarded-For carries the client
            fwd = self.headers.get("X-Forwarded-For", "")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def end_headers(self):
        if self.production:
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
        origin = self.headers.get("Origin") if hasattr(self, "headers") and self.headers else None
        if origin and ("*" in config.CORS_ORIGINS or origin in config.CORS_ORIGINS):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        super().end_headers()

    def _request_lang(self) -> str:
        """The visitor's language before the body is read (for answers like 429): ?lang=, then the pages'
        X-Poecraft-Lang header (the language chosen in the UI), then the browser's Accept-Language."""
        known = {l["code"] for l in i18n.languages()}          # every poecraft/locales/<code>.json
        want = parse_qs(urlparse(self.path).query).get("lang", [None])[0] or self.headers.get("X-Poecraft-Lang")
        if want in known:
            return want
        for part in (self.headers.get("Accept-Language") or "").split(","):
            code = part.split(";")[0].strip().lower()[:2]
            if code in known:
                return code
        return i18n.DEFAULT

    def _request_lang(self) -> str:
        """The visitor's language before the body is read (for answers like 429): ?lang=, then the pages'
        X-Poecraft-Lang header (the language chosen in the UI), then the browser's Accept-Language."""
        known = set(i18n.available_codes()) if hasattr(i18n, "available_codes") else {"en", "ru", "de"}
        want = parse_qs(urlparse(self.path).query).get("lang", [None])[0] or self.headers.get("X-Poecraft-Lang")
        if want in known:
            return want
        for part in (self.headers.get("Accept-Language") or "").split(","):
            code = part.split(";")[0].strip().lower()[:2]
            if code in known:
                return code
        return i18n.DEFAULT

    def _admit(self) -> bool:
        """Production rate limit for this client and endpoint; answers 429 itself when over."""
        if not self.production:
            return True
        path = urlparse(self.path).path
        if not path.startswith("/api/") or path == "/api/health":
            return True
        ok, wait = _LIMITS[_LIMITED.get(path, "api")].allow((self._client_ip(), _LIMITED.get(path, "api")))
        if ok:
            return True
        body = json.dumps({"error": M("m.srv.rate_limited", s=max(1, round(wait))).render(self._request_lang())}).encode()
        self.send_response(429)
        self.send_header("Retry-After", str(max(1, round(wait))))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def do_OPTIONS(self):                     # CORS preflight (only for origins in POECRAFT_CORS_ORIGINS)
        origin = self.headers.get("Origin")
        allowed = origin and ("*" in config.CORS_ORIGINS or origin in config.CORS_ORIGINS)
        self.send_response(204 if allowed else 403)
        if allowed:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Poecraft-Lang")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        if not self._admit():
            return
        url = urlparse(self.path)
        q = parse_qs(url.query)
        try:
            if url.path == "/api/health":            # for the container health check / uptime monitors
                return self._json({"ok": True, "version": __version__, "data": config.DB_PATH.exists(),
                                   "production": self.production})
            if url.path == "/api/meta":
                data = get_data()
                rules = get_rules()
                lang = q.get("lang", [i18n.DEFAULT])[0]
                classes = sorted({b.item_class for b in data.bases.values()})
                return self._json({"version": __version__, "data": data.meta, "item_classes": classes,
                                   "influences": list(rules["influences"]),
                                   "features": render(feature_rows(rules) + special.feature_rows(get_special(), rules),
                                                      lang),
                                   "max_influences": rules["max_influences"], "ocr": ocr.status()})
            if url.path == "/api/bases":
                data = get_data()
                lang = q.get("lang", [i18n.DEFAULT])[0]
                text, cls = q.get("q", [""])[0], q.get("cls", [None])[0] or None
                bases = data.find_bases(text, cls, limit=80)
                if lang != i18n.DEFAULT and text.strip():
                    # also match the game's own localized base names (by id); English names keep working
                    want = text.strip().lower()
                    seen = {b.id for b in bases}
                    local = [b for b in data.find_bases("", cls, limit=len(data.bases))
                             if b.id not in seen and want in i18n.game_text("base", b.id, b.name, lang)[0].lower()]
                    bases = (bases + local)[:80]
                rows = []
                for b in bases:
                    name, ok = i18n.game_text("base", b.id, b.name, lang)
                    rows.append({"id": b.id, "name": name, "item_class": b.item_class, "drop_level": b.drop_level,
                                 "_en": ([] if ok else ["name"]) + ([] if lang == i18n.DEFAULT else ["item_class"])})
                return self._json(rows)
            if url.path == "/api/locales":           # every poecraft/locales/<code>.json - no code change to add one
                return self._json(i18n.languages())
            if url.path == "/api/locale":            # UI strings; the browser falls back to "en" with a marker
                code = q.get("lang", [i18n.DEFAULT])[0]
                return self._json({"code": code, "strings": i18n.strings(code), "en": i18n.strings(i18n.DEFAULT),
                                   "game_data": i18n.game_data_status(code), "game_names": _game_names(code)})
            if url.path == "/api/prices":
                return self._json(_localized_prices(Prices(league=q.get("league", [None])[0]).status_json(), q.get("lang", [i18n.DEFAULT])[0]))
            if url.path == "/api/families":
                return self._json(_families_cached(q))
            if url.path == "/api/timeless/tree":       # the passive tree's points, for the Timeless socket picker
                from .data import timeless as td
                if not td.available():
                    return self._json({"error": "timeless data not downloaded"}, 404)
                from .engine.timeless import tree_points
                return self._json(tree_points())
            if url.path == "/api/trade/leagues":       # league list of the trade site + the current one
                from .data import trade
                from .engine.tradelink import _price_league
                return self._json(trade.current_league(_price_league()))
            if url.path == "/api/modsearch":          # matches on this base + matching mods that cannot roll here
                fam = _families_cached(q)
                data = get_data()
                return self._json(mod_search(data, data.bases[q["base"][0]], fam["families"], q.get("q", [""])[0],
                                             q.get("lang", [i18n.DEFAULT])[0]))
            # pages: "/" (and "/hub") is the TensChep Workshop hub, "/craft" the crafting planner
            if url.path == "/craft/":
                self.send_response(301)
                self.send_header("Location", "/craft")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = PAGES.get(url.path) or url.path.lstrip("/")
            file = (config.WEB_DIR / path).resolve()
            if config.WEB_DIR.resolve() not in file.parents or not file.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self._send(200, file.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
        except (KeyError, ValueError) as e:
            return self._json({"error": _error(e, q.get("lang", [i18n.DEFAULT])[0])}, 400)
        except Exception:  # noqa: BLE001 - report to the UI instead of dropping the connection (details in the log)
            traceback.print_exc()
            return self._json({"error": M("m.srv.internal").render(q.get("lang", [i18n.DEFAULT])[0])}, 500)

    def do_POST(self):
        if not self._admit():
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
        except ValueError:
            size = -1
        if size < 0 or size > config.MAX_BODY:
            return self._json({"error": f"request body larger than {config.MAX_BODY} bytes"}, 413)
        if urlparse(self.path).path == "/api/prices/update":
            if not self.production:      # public server: prices refresh in the background, never per click
                ninja.update()           # respects the on-disk cache, so repeated clicks do not hit poe.ninja
            lang = parse_qs(urlparse(self.path).query).get("lang", [i18n.DEFAULT])[0]
            return self._json(_localized_prices(Prices().status_json(), lang))
        if urlparse(self.path).path == "/api/evaluate":
            # Item Evaluator: listings for a pasted item from the trade site (cached 5 min; searches are rate-limited)
            lang = i18n.DEFAULT
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                lang = body.get("lang", i18n.DEFAULT)
                from .engine.evaluator import evaluate
                socket = str(body["socket"]) if body.get("socket") else None      # Timeless Jewel calculator
                eq = body.get("equivalents")                                     # "Find Equivalent Seeds"
                eq = [str(x) for x in eq][:100] if isinstance(eq, list) else None
                return self._json(render(evaluate(str(body.get("text", "")), league=body.get("league") or None,
                                                  socket=socket, equivalents=eq), lang))
            except (KeyError, ValueError, TypeError) as e:
                if isinstance(e, TypeError):
                    traceback.print_exc()
                return self._json({"error": _error(e, lang)}, 400)
            except Exception:  # noqa: BLE001 - a bug: log it, answer in the user's language, keep the connection
                traceback.print_exc()
                return self._json({"error": M("m.srv.internal").render(lang)}, 500)
        if urlparse(self.path).path == "/api/trade":
            # "Find on Trade": the targets as a search on the official trade site (asks the site for a short link)
            lang = i18n.DEFAULT
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                lang = body.get("lang", i18n.DEFAULT)
                from .engine.tradelink import trade_search
                r = trade_search(body["base"], int(body.get("ilvl", 86)), body.get("influences", []),
                                 body.get("targets", []), league=body.get("league") or None)
                return self._json(render(r, lang))
            except (KeyError, ValueError, TypeError) as e:
                if isinstance(e, TypeError):     # usually a malformed body - but log it: it can be a bug
                    traceback.print_exc()
                return self._json({"error": _error(e, lang)}, 400)
            except Exception:  # noqa: BLE001 - a bug: log it, answer in the user's language, keep the connection
                traceback.print_exc()
                return self._json({"error": M("m.srv.internal").render(lang)}, 500)
        if urlparse(self.path).path in ("/api/import", "/api/ocr"):
            # an item from the game's Ctrl+C text, or (beta) from a screenshot via the configured OCR service
            lang = i18n.DEFAULT
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                lang = body.get("lang", i18n.DEFAULT)
                if urlparse(self.path).path == "/api/import":
                    return self._json(_imported(parse_item_text(body.get("text", "")), lang))
                if not ocr.status()["available"]:
                    return self._json({"error": M("m.imp.ocr_unavailable").render(lang)}, 501)
                try:
                    text = ocr.image_to_text(body.get("image", ""), lang)
                except ocr.OcrError as e:
                    return self._json({"error": M("m.imp.ocr_failed", error=e.args[0] if e.args else str(e)).render(lang)}, 502)
                result = parse_item_text(text)
                result["warnings"].insert(0, M("m.imp.ocr"))
                result["ocr_text"] = text                      # shown in the paste box, so the user can check it
                return self._json(_imported(result, lang))
            except (KeyError, ValueError, TypeError) as e:
                if isinstance(e, TypeError):     # usually a malformed body - but log it: it can be a bug
                    traceback.print_exc()
                return self._json({"error": _error(e, lang)}, 400)
            except Exception:  # noqa: BLE001 - a bug: log it, answer in the user's language, keep the connection
                traceback.print_exc()
                return self._json({"error": M("m.srv.internal").render(lang)}, 500)
        if urlparse(self.path).path == "/api/localize":
            # the same computed plan / recovery in another language - re-rendered, never re-planned
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                lang = body.get("lang", i18n.DEFAULT)
                rid = body.get("result_id")
                if rid is not None:
                    raw = _RESULTS.get(rid)
                    if raw is None:
                        return self._json({"error": M("m.srv.expired").render(lang)}, 410)
                    return self._json(_present(raw, lang, rid))
                result = body["result"]                       # an already rendered (English) result
                fn = i18n.localize_recovery if "have" in result else i18n.localize_plan
                return self._json(fn(result, lang))
            except (KeyError, ValueError, TypeError) as e:
                if isinstance(e, TypeError):     # usually a malformed body - but log it: it can be a bug
                    traceback.print_exc()
                return self._json({"error": _error(e, self._request_lang())}, 400)
            except Exception:  # noqa: BLE001 - a bug: log it, answer in the user's language, keep the connection
                traceback.print_exc()
                return self._json({"error": M("m.srv.internal").render(self._request_lang())}, 500)
        if urlparse(self.path).path not in ("/api/plan", "/api/recover"):
            return self._send(404, b"not found", "text/plain")
        lang = i18n.DEFAULT
        stream = None
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            lang = body.get("lang", i18n.DEFAULT)
            sims = max(0, min(int(body.get("sims", config.DEFAULT_SIMULATIONS)), MAX_SIMS))
            if body.get("stream"):
                stream = self._start_stream()
            if not _plan_lock.acquire(timeout=config.PLAN_QUEUE_WAIT):
                raise Busy()
            try:
                planner = Planner(sims=sims, progress=stream)
                if urlparse(self.path).path == "/api/recover":
                    raw = planner.recover(body["base"], int(body.get("ilvl", 86)), body.get("influences", []),
                                          body.get("targets", []), body.get("have", []),
                                          full_sims=bool(body.get("full_sims", False)), lang=None)
                else:
                    raw = planner.plan(body["base"], int(body.get("ilvl", 86)), body.get("influences", []),
                                       body.get("targets", []), objective=body.get("objective", "min_cost"),
                                       lang=None)
            finally:
                _plan_lock.release()
            return self._reply(stream, _present(raw, lang, _keep(raw)))
        except Busy:
            return self._reply(stream, {"error": M("m.srv.busy").render(lang)}, 503)
        except Infeasible as e:      # no plan is fabricated: say what is reachable and why the rest is not
            return self._reply(stream, {"error": e.render(lang), "infeasible": e.render_report(lang)}, 422)
        except (KeyError, ValueError) as e:
            return self._reply(stream, {"error": _error(e, lang)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._reply(stream, {"error": f"{type(e).__name__}: {e}"}, 500)

    # ---- streamed planning (body "stream": true): NDJSON, one {"progress": ...} line per update, then one
    # {"result": ...} or {"error": ..., "status": code} line. HTTP/1.0 without Content-Length: the response ends
    # when the connection closes, so the browser can read lines as they arrive.
    def _start_stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def send(event: dict) -> None:
            self._line({"progress": event})
        return send

    def _line(self, obj) -> None:
        try:
            self.wfile.write(json.dumps(obj, default=str).encode("utf-8") + b"\n")
            self.wfile.flush()
        except OSError:          # the browser went away: keep computing quietly, nobody is reading
            pass

    def _reply(self, stream, obj: dict, code: int = 200) -> None:
        if stream is None:
            return self._json(obj, code)
        self._line({"result": obj} if code == 200 else dict(obj, status=code))


class BoundedServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with at most `max_connections` handler threads; further connections get a quick 503."""
    daemon_threads = True

    def __init__(self, addr, handler, max_connections: int):
        super().__init__(addr, handler)
        self._slots = threading.BoundedSemaphore(max_connections)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: text/plain\r\n"
                                b"Content-Length: 4\r\nConnection: close\r\nRetry-After: 5\r\n\r\nbusy")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, production: bool = False) -> None:
    get_data()  # load once up front so errors (missing DB) surface immediately
    Handler.production = production
    httpd = BoundedServer((host, port), Handler, config.MAX_CONNECTIONS)
    url = f"http://{host}:{port}/"
    print(f"PoE crafting planner running at {url}  ({'production' if production else 'local'} mode; Ctrl+C to stop)")
    if production and not (config.CONTACT or config.USER_AGENT_OVERRIDE):
        print("! POECRAFT_CONTACT is not set: GGG and poe.ninja ask API users to put a contact in the User-Agent.",
              flush=True)

    def refresh_prices():
        status = ninja.update()
        if status["state"] == "live":
            print(f"Live prices loaded from poe.ninja ({status['league']}, {status['fetched_at']}).", flush=True)
        if status.get("message"):
            print(f"! {status['message']}", flush=True)

    def refresh_loop():                # production: prices are refreshed here, never by a visitor's click
        while True:
            refresh_prices()
            time.sleep(max(5, config.PRICE_REFRESH_MIN) * 60)

    threading.Thread(target=refresh_loop if production else refresh_prices, daemon=True).start()
    if open_browser and not production:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
