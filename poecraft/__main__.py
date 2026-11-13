"""Command line: python -m poecraft <command>

  update                     download RePoE data and rebuild the database (new league = run this)
  fetch [--from-dir DIR]     only download (or import manually downloaded files)
  build                      only rebuild data/poecraft.db from data/raw/
  serve [--port 8765]        start the local web UI (refreshes live prices in the background)
  prices update|show [--league L]   fetch/cache live poe.ninja prices, or show the current status
  bases QUERY                search bases
  mods --base NAME           list mods that can appear on a base
  plan --base NAME --target "TEXT[>=MIN]" ...   print a crafting plan
                             Eldritch implicit target: "TEXT (Eldritch, Searing Exarch)>=MIN"
                             (or "eldritch-exarch:TEXT", "eldritch-eater:TEXT")
                             Influenced target (Shaper/Elder/Conqueror Exalt): "TEXT (Hunter)>=MIN"
                             Fractured target (Fracturing Orb first): "fractured:TEXT>=MIN"
                             Veiled target (Aisling / Veiled Chaos, then unveil): "TEXT (Veiled)>=MIN"
                             Elevated target (Orb of Dominance, range plan): "TEXT (Elevated Elder)>=MIN"
                             Special-fossil target: "TEXT (Hollow)>=MIN", "TEXT (Faceted)>=MIN"; Beastcraft: "TEXT (Beastcraft)"
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import config


_ELDRITCH_MARK = re.compile(r"\(\s*eldritch\s*,\s*(searing exarch|exarch|eater of worlds|eater)\s*\)", re.I)


def _eldritch_target(ctx, text: str, mn: str) -> dict | None:
    """'...(Eldritch, Searing Exarch)' / '...(Eldritch, Eater of Worlds)' or 'eldritch-exarch:...' /
    'eldritch-eater:...' -> an Eldritch implicit target spec; None for ordinary explicit targets."""
    side = None
    m = _ELDRITCH_MARK.search(text)
    if m:
        side = "exarch" if "exarch" in m.group(1).lower() else "eater"
        text = _ELDRITCH_MARK.sub("", text).strip()
    for prefix, s in (("eldritch-exarch:", "exarch"), ("eldritch-eater:", "eater")):
        if text.lower().startswith(prefix):
            side, text = s, text[len(prefix):].strip()
    if side is None:
        return None
    from .data.eldritch import SIDE_NAMES, get_eldritch
    from .engine import eldritch
    data = get_eldritch()
    q = text.lower()
    on_base = eldritch.base_families(data, ctx.base.item_class, ctx.tags)
    # match against every Eldritch implicit of that side, so a base that cannot have it gets a clear reason
    cands = [f for f in eldritch.all_families(data).values() if f.side == side and q in f.label.lower()]
    if not cands:
        other = [f for f in eldritch.all_families(data).values() if f.side != side and q in f.label.lower()]
        if not other:
            raise SystemExit(_t("cli.res.no_implicit", side=SIDE_NAMES[side], text=text))
        # exists only on the other side: pass it on so the planner explains the mix-up in its report
        f = other[0]
        return {"eldritch": side, "signature": side + f.key[f.key.find("|"):], "label": f.label,
                "min": float(mn) if mn.strip() else None}
    exact = [f for f in cands if f.label.lower() == q]
    best = (exact or sorted(cands, key=lambda f: (f.key not in on_base, len(f.label))))[0]
    if len(cands) > 1 and not exact:
        print("  " + _t("cli.res.using_implicit", text=text, side=SIDE_NAMES[side], label=best.label,
                        others=", ".join(f.label for f in cands if f is not best)[:200]))
    return {"eldritch": side, "signature": best.key, "min": float(mn) if mn.strip() else None, "label": best.label}


_INFLUENCE_MARK = re.compile(r"\(\s*(shaper|elder|crusader|hunter|redeemer|warlord)\s*\)", re.I)


def _influence_families(ctx, inf: str):
    """Families an influence Exalt can add on this base (its influence pool)."""
    from .data.repository import get_data, get_rules
    from .engine import influence
    c = influence.influence_context(get_data(), get_rules(), ctx.base, ctx.ilvl, (inf,))
    sigs = {e.mod.signature for e in influence.exalt_pool(c)}
    return [f for f in c.families().values() if f.signature in sigs]


def _influence_target(ctx, text: str, mn: str) -> dict | None:
    """'+#% to Fire Resistance (Hunter)' -> an influence target (crafted with that influence's Exalted Orb)."""
    m = _INFLUENCE_MARK.search(text)
    if not m:
        return None
    inf, text = m.group(1).lower(), _INFLUENCE_MARK.sub("", text).strip()
    q = text.lower()
    cands = [f for f in _influence_families(ctx, inf) if q in f.label.lower()]
    if not cands:
        # not in this base's pool for that influence: pass the matching stat line on, so the plan explains why.
        # Prefer ordinary mods of that influence (any item class), then any influence mod, then anything.
        from .data.repository import get_data, get_rules
        from .engine.context import generic_text
        suffix = get_rules()["influences"][inf]
        mods = [x for x in get_data().mods.values() if x.domain == "item" and q in generic_text(x.text).lower()]

        def rank(x):
            own = any(w > 0 and t.endswith(suffix) for t, w in x.spawn_weights)
            anyinf = any(w > 0 and t.endswith(tuple(get_rules()["influences"].values())) for t, w in x.spawn_weights)
            return (not own, not anyinf, len(x.text))
        mod = min(mods, key=rank, default=None)
        if mod is None:
            raise SystemExit(_t("cli.res.no_modifier", text=text))
        return {"influence": inf, "signature": mod.signature, "label": generic_text(mod.text),
                "min": float(mn) if mn.strip() else None}
    exact = [f for f in cands if f.label.lower() == q]
    best = (exact or sorted(cands, key=lambda f: len(f.label)))[0]
    if len(cands) > 1 and not exact:
        print("  " + _t("cli.res.using_influence", text=text, inf=inf.title(), label=best.label,
                        others=", ".join(f.label for f in cands if f is not best)[:200]))
    return {"influence": inf, "signature": best.signature, "min": float(mn) if mn.strip() else None,
            "label": best.label}


_VEILED = re.compile(r"\s*\(veiled\)\s*", re.I)


def _veiled_target(ctx, spec: str) -> dict | None:
    """'TEXT (Veiled)>=MIN' or 'veiled:TEXT>=MIN': an unveiled modifier (Aisling / Veiled Chaos Orb, then unveil)."""
    text, _, mn = spec.partition(">=")
    text = text.strip()
    if text.lower().startswith("veiled:"):
        text = text[len("veiled:"):].strip()
    elif _VEILED.search(text):
        text = _VEILED.sub(" ", text).strip()
    else:
        return None
    from .data.veiled import get_veiled
    from .engine import veiled
    vdata = get_veiled()
    fams = list(veiled.all_families(vdata).values())
    q = text.lower()
    cands = [f for f in fams if q in f.label.lower()]
    if not cands:
        raise SystemExit(f"No unveiled modifier matches '{text}'. Veiled outcomes on this base: "
                         + "; ".join(sorted({veiled.all_families(vdata)[m.signature].label
                                             for side in ("prefix", "suffix")
                                             for m, _ in veiled.unveil_pool(vdata, ctx, side)}))[:600])
    here = {m.signature for side in ("prefix", "suffix") for m, _ in veiled.unveil_pool(vdata, ctx, side)}
    exact = [f for f in cands if f.label.lower() == q]
    # exact text first, then outcomes this base can get, then generic (Aisling / Veiled Chaos) ones
    best = (exact or sorted(cands, key=lambda f: (f.signature not in here, not f.generic, len(f.label))))[0]
    if len(cands) > 1 and not exact:
        print(f"  '{text}' -> using veiled '{best.label}' ({best.affix}); "
              f"other matches: {', '.join(f.label for f in cands if f is not best)[:200]}")
    return {"veiled": True, "signature": best.signature, "label": best.label,
            "min": float(mn) if mn.strip() else None}


_ELEVATED = re.compile(r"\s*\(elevated (shaper|elder|crusader|hunter|redeemer|warlord)\)\s*", re.I)


def _elevated_target(ctx, spec: str) -> dict | None:
    """'TEXT (Elevated Elder)>=MIN': an Elevated modifier (Orb of Dominance range plan)."""
    text, _, mn = spec.partition(">=")
    hit = _ELEVATED.search(text)
    if not hit:
        return None
    inf, text = hit.group(1).lower(), _ELEVATED.sub(" ", text).strip()
    from .data.elevated import get_elevated
    from .engine.context import generic_text
    mods = [e for e in get_elevated().mods if e.influence == inf]
    q = text.lower()
    cands = [e for e in mods if q in generic_text(e.mod.text).lower()]
    if not cands:
        raise SystemExit(f"No Elevated {inf.title()} modifier matches '{text}'.")
    exact = [e for e in cands if generic_text(e.mod.text).lower() == q]
    best = (exact or sorted(cands, key=lambda e: (e.base_type is None, len(e.mod.text))))[0]
    label = generic_text(best.mod.text)
    if len({e.mod.signature for e in cands}) > 1 and not exact:
        print(f"  '{text}' -> using Elevated {inf.title()} '{label}'")
    return {"elevated": inf, "signature": best.mod.signature, "label": label,
            "min": float(mn) if mn.strip() else None}


def _special_names() -> dict[str, str]:
    """'(Faceted)' -> 'Faceted Fossil' for every special fossil, plus '(Beastcraft)'."""
    from .data.repository import get_rules
    return {n.split()[0].lower(): n for n in get_rules()["fossils_not_modelled"]}


def _special_target(ctx, spec: str, marked: bool = True) -> dict | None:
    """'TEXT (Faceted)>=MIN' / 'TEXT (Beastcraft)>=MIN'. With marked=False, an unmarked target that only a supported
    special fossil or a Beastcraft outcome provides (the planner then says whether that route is supported)."""
    from .data.special import get_special
    from .engine import special
    from .engine.context import generic_text
    text, _, mn = spec.partition(">=")
    text = text.strip()
    names = _special_names()
    kind = None
    hit = re.search(r"\s*\(([A-Za-z]+)\)\s*$", text)
    if marked:
        if not hit or (hit.group(1).lower() not in names and hit.group(1).lower() != "beastcraft"):
            return None
        kind, text = hit.group(1).lower(), text[:hit.start()].strip()
    data = get_special()
    q = text.lower()
    mn_v = float(mn) if mn.strip() else None
    if kind in (None, "beastcraft"):
        beast = next((m for m in data.beast_outcomes if q in generic_text(m.text).lower()), None)
        if beast is not None or kind == "beastcraft":
            return {"beastcraft": True, "signature": beast.signature if beast else None,
                    "label": generic_text(beast.text) if beast else text, "min": mn_v}
    fossils = [names[kind]] if kind else [n for n, f in data.fossils.items()
                                          if special.verdict(f, get_rules_cached())[0] == special.SUPPORTED]
    for name in fossils:
        f = data.fossils.get(name)
        mods = (f.added + f.forced) if f else ()
        m = next((m for m in mods if q in generic_text(m.text).lower()), None)
        if m is not None or kind:
            return {"fossil": name, "signature": m.signature if m else None,
                    "label": generic_text(m.text) if m else text, "min": mn_v}
    return None


def get_rules_cached():
    from .data.repository import get_rules
    return get_rules()


def _targets_from_cli(ctx, specs: list[str]) -> list[dict]:
    """'fractured:' in front of any target marks it as the mod to fracture with a Fracturing Orb;
    'veiled:' in front, or '(Veiled)' after the text, asks for an unveiled modifier."""
    veiled_at = {}
    rest = []
    for i, spec in enumerate(specs):
        v = _elevated_target(ctx, spec) or _veiled_target(ctx, spec) or _special_target(ctx, spec)
        if v is not None:
            veiled_at[i] = v
        else:
            rest.append(spec)
    fractured = []
    plain = []
    for spec in rest:
        flag = spec.strip().lower().startswith("fractured:")
        fractured.append(flag)
        plain.append(spec.strip()[len("fractured:"):].strip() if flag else spec)
    resolved = _resolve_targets(ctx, plain)
    for spec, flag in zip(resolved, fractured):
        if flag:
            spec["fractured"] = True
    it = iter(resolved)
    return [veiled_at[i] if i in veiled_at else next(it) for i in range(len(specs))]


def _resolve_targets(ctx, specs: list[str]) -> list[dict]:
    fams = list(ctx.families().values())
    out = []
    for spec in specs:
        text, _, mn = spec.partition(">=")
        eld = _eldritch_target(ctx, text.strip(), mn)
        if eld is not None:
            out.append(eld)
            continue
        infl = _influence_target(ctx, text.strip(), mn)
        if infl is not None:
            out.append(infl)
            continue
        affix = None
        text = text.strip()
        for a in ("prefix:", "suffix:"):
            if text.lower().startswith(a):
                affix, text = a[:-1], text[len(a):].strip()
        q = text.lower()
        cands = [f for f in fams if q in f.label.lower() and (affix is None or f.affix == affix)]
        exact_inf = None
        if not any(f.label.lower() == q for f in cands):
            # an exact influence-only modifier beats a partial match on an ordinary one
            exact_inf = next((f for inf in ("shaper", "elder", "crusader", "hunter", "redeemer", "warlord")
                              for f in _influence_families(ctx, inf) if f.label.lower() == q), None)
        if exact_inf is not None:
            out.append({"signature": exact_inf.signature, "min": float(mn) if mn.strip() else None,
                        "label": exact_inf.label})
            continue
        if not cands:
            # maybe an influence-only modifier: let the planner say which influence it needs
            for inf in ("shaper", "elder", "crusader", "hunter", "redeemer", "warlord"):
                hit = next((f for f in _influence_families(ctx, inf) if q in f.label.lower()), None)
                if hit:
                    out.append({"signature": hit.signature, "min": float(mn) if mn.strip() else None,
                                "label": hit.label})
                    break
            else:
                sp = _special_target(ctx, text + (f">={mn}" if mn.strip() else ""), marked=False)
                if sp is None:
                    raise SystemExit(_t("cli.res.no_mod", text=text))
                print("  " + _t("cli.res.only_from", text=text,
                                source=_t("cli.res.beastcrafting") if sp.get("beastcraft") else sp["fossil"]))
                out.append(sp)
            continue
        exact = [f for f in cands if f.label.lower() == q]
        # prefer exact text, then the most common (highest roll weight) stat line
        best = (exact or sorted(cands, key=lambda f: (-("roll" in f.sources),
                                                      -sum(t["weight"] for t in f.tiers), len(f.label))))[0]
        if len(cands) > 1 and not exact:
            print("  " + _t("cli.res.using", text=text, label=best.label, affix=best.affix,
                            others=", ".join(f.label for f in cands if f is not best)[:200]))
        out.append({"signature": best.signature, "min": float(mn) if mn.strip() else None, "label": best.label})
    return out


LANG = "en"        # output language (--lang): labels, plan explanations and game data (by internal id)


def _t(key: str, /, **kw) -> str:
    from . import i18n
    return i18n.t(LANG, key, **kw)


def _import_paste(args, data):
    """--paste / --paste-file: parse the game's item text; sets the base, item level and influences on `args` and
    returns the parse result (None without a paste). Warnings are printed; nothing unknown stops the import."""
    if not (args.paste or args.paste_file):
        return None
    from pathlib import Path
    from .data.repository import get_rules
    from .engine.itemtext import parse_item_text
    from .messages import render
    text = args.paste.replace("\\n", "\n") if args.paste else Path(args.paste_file).read_text(encoding="utf-8")
    r = render(parse_item_text(text, data, get_rules()), args.lang)
    for w in r["warnings"]:
        print(f"! {w}")
    if not r.get("base_id"):
        return r
    args.ilvl = r["ilvl"]
    args.influence = sorted(set(args.influence) | set(r["influences"]))
    if args.paste_as == "have":
        args.have = list(args.have) + [h["id"] for h in r["have"]]
    from . import i18n
    name, _ = i18n.game_text("base", r["base_id"], r["base_name"], args.lang)
    # this runs before the CLI's output language is set: use the requested one directly
    print(i18n.t(args.lang, "cli.imported", base=name, ilvl=r["ilvl"], n=len(r["targets"]), have=len(r["have"]),
                 mode=args.paste_as))
    return r


def _spec_from_import(t: dict) -> dict:
    spec = {"signature": t["signature"], "min": t["min"], "label": t["label"]}
    if t.get("eldritch"):
        spec["eldritch"] = t["eldritch"]
    if t.get("fractured"):
        spec["fractured"] = True
    return spec


def _cur(name: str) -> str:
    """A currency name for a label, from the game's own localization by item id ("[EN] " if it has none)."""
    from . import i18n
    return i18n.mark(*i18n.currency_text(name, LANG))


def _progress_printer(stream=None):
    """Progress lines on stderr while planning, e.g. "[  1.4s] Calculating strategies… 12/17 · ~2s remaining".
    stdout stays the plan only, so --json output and redirects are unaffected."""
    from .engine.progress import Progress
    stream = stream or sys.stderr
    state = {"timeouts": 0}

    def show(p: dict) -> None:
        if p["phase"] != "done" and not p["total"]:
            return                     # nothing counted yet: no "0/0" line
        if p["phase"] == "done":
            line = _t("progress.done") + " · " + _t("progress.elapsed", s=f"{p['elapsed']:.1f}")
        else:
            line = _t(f"progress.{p['phase']}", done=p["done"], total=max(p["total"], p["done"]))
            if p["elapsed"] > 1 and p["eta"] is not None:
                line += " · " + _t("progress.eta", s=max(1, round(p["eta"])))
        if p["timeouts"] > state["timeouts"]:
            state["timeouts"] = p["timeouts"]
            line += "  ! " + _t("progress.timeout")
        print(f"[{p['elapsed']:6.1f}s] {line}", file=stream, flush=True)
    return Progress(show, min_interval=0.5)


def _status(s: str) -> str:
    from . import i18n
    text, fell_back = i18n.lookup(LANG, f"status.{s}")
    return s if text == f"status.{s}" else ((i18n.FALLBACK_MARK if fell_back else "") + text)


def _print_markers(result: dict | None = None) -> None:
    """Where game data comes from in this language, and which explanation keys (if any) are not translated yet."""
    from . import i18n
    if i18n.game_data_status(LANG)["available"]:
        print(_t("gamedata.marker"))
    else:
        print(f"[EN] {_t('gamedata.missing')}")
    missing = (result or {}).get("untranslated_keys") or []
    if missing:
        print(f"[EN] {_t('cli.untranslated', n=len(missing), keys=', '.join(missing[:12]))}")


def _present(raw: dict) -> dict:
    """A language-free planner result in the CLI language: explanations rendered from their message keys, game data
    by internal id, plus the message keys that had to fall back to English."""
    from . import i18n
    from .messages import missing_keys, render
    out = render(raw, LANG)
    out = i18n.localize_recovery(out, LANG) if "have" in out else i18n.localize_plan(out, LANG, targets=False)
    return dict(out, untranslated_keys=missing_keys(raw, LANG))


def _g(obj: dict, field: str) -> str:
    """A game-data field as localized by i18n.localize_*: "[EN] " when it stayed English."""
    return ("[EN] " if field in obj.get("_en", ()) else "") + str(obj.get(field, ""))


def _print_plan(result: dict, show_all: bool = False) -> None:
    item = result["item"]
    _print_markers(result)
    inf = _t("cli.influences", list=", ".join(item["influences"])) if item["influences"] else ""
    print("\n" + _t("cli.item", base=_g(item, "base"), cls=_g(item, "item_class"), ilvl=item["ilvl"], inf=inf))
    print(_t("cli.targets"))
    for t in result["targets"]:
        if t.get("eldritch") or t.get("influence") or t.get("veiled") or t.get("elevated") or t.get("special_fossil"):
            print(f"  - [{t['affix']}] {_g(t, 'description')}   {_t('cli.sources')}: {', '.join(t['sources'])}")
            continue
        src = [_t("source.roll")] if t["roll_chance_single"] else []
        src += [_t("source.essence")] if t["essences"] else []
        src += [_t("source.bench")] if t["bench"] else []
        print(f"  - [{t['affix']}] {_g(t, 'description')}   {_t('cli.sources')}: {', '.join(src)}")
    pr = result.get("prices", {})
    if pr.get("state") == "live":
        print(_t("cli.pricesLive", league=pr["league"], at=pr["fetched_at"], n=pr["live_count"]))
    elif pr.get("state") == "stale":
        print(_t("cli.pricesStale", league=pr["league"], at=pr["fetched_at"]))
    else:
        st = pr.get("state", "unavailable")
        from . import i18n
        known = i18n.lookup(LANG, f"prices.state.{st}")[0] != f"prices.state.{st}"
        print(_t("cli.pricesEst", state=_t(f"prices.state.{st}") if known else st))
    obj = result["objective"]
    print(_t("cli.objective", key=obj["key"], desc=_t(f"objective.{obj['key']}.desc")))
    for w in result["warnings"]:
        print(f"! {w}")
    strategies = result["strategies"]
    for i, s in enumerate(strategies if show_all else strategies[:1]):
        tag = _t("cli.schematic" if result.get("schematic") else "cli.recommended") if i == 0 else _t("cli.other", i=i)
        flag = "" if s["status"] == "computed" else f"  [{_status(s['status']).upper()}]"
        print(f"\n=== {tag}: {s['name']}{flag} ===")
        cb, m = s["cost_breakdown"], s["metrics"]
        eld = f" + Eldritch {cb['eldritch']:,.1f}" if cb.get("eldritch") else ""
        eld += f" + influence {cb['influence']:,.1f}" if cb.get("influence") else ""
        eld += f" + Fracturing Orb phase {cb['fracture']:,.1f}" if cb.get("fracture") else ""
        eld += f" + veiled part (Aisling, Annulments, Alchemy) {cb['veiled']:,.1f}" if cb.get("veiled") else ""
        parts = f"{_t('cli.rolling')} {cb['rng']:,.1f} + {_t('cli.bench')} {cb['bench']:,.1f}{eld}"
        cr = s.get("cost_range")
        if cr:          # odds missing from the game data: a range, never a single number
            print(f"{_t('cli.expected')}: {cr['text']}")
            print(f"  {_t('cli.lowerEnd')}: {cr['low_reason']}")
            if cr.get("assumed_reason"):
                print(f"  {_t('cli.middle')}: {cr['assumed_reason']}")
            print(f"  {_t('cli.upperEnd')}: {cr['high_reason']}")
            dom = f" + Orb of Dominance {cb['dominance']:,.1f}" if cb.get("dominance") else ""
            print(f"  {_t('cli.bestCaseOf')}: {parts}{dom}")
            print(_t("cli.badLuckNone"))
        else:
            print(f"{_t('cli.expected')}: {s['expected_cost']:,.1f} {_t('cli.chaos')}  ({parts})")
            od = s.get("optional_divine")
            if od:
                print("  " + _t("cli.optionalDivine", c=f"{od['cost']:,.1f}", n=f"{od['divines']:.2f}",
                                p=f"{od['p_values_met'] * 100:.1f}", total=f"{od['total_with']:,.1f}"))
            print(_t("cli.badLuck", c=f"{m['bad_luck_cost']:,.1f}", n=f"{m['applications']:,.1f}"))
        if result.get("cost_note"):
            print(result["cost_note"])
        sim = s["sim"]
        if sim:
            simparts = [sim] if "n" in sim else [sim["alt"], sim["chain"]]
            labels = [_t("cli.attempt")] if "n" in sim else [
                _t("cli.altStage", alteration=_cur("Orb of Alteration")),
                _t("cli.chainStage", regal=_cur("Regal Orb"), exalt=_cur("Exalted Orb"))]
            for label, part in zip(labels, simparts):
                print(_t("cli.simLine", label=label, a=f"{part['analytic'] * 100:.4g}", s=f"{part['p'] * 100:.4g}",
                         hits=part["hits"], n=part["n"], lo=f"{part['ci'][0] * 100:.3g}", hi=f"{part['ci'][1] * 100:.3g}")
                      + ("" if part["reliable"] else _t("cli.fewHits"))
                      + (_t("cli.lowConf", why=part["low_confidence"]) if part.get("low_confidence") else ""))
        if i == 0:
            print(f"{_t('cli.why')}: {result['selection_note']}")
        _print_steps(s["steps"])
        for note in s["notes"]:
            print(f"  {_t('cli.note')}: {note}")
    rows = result["comparison"]
    print("\n" + _t("cli.comparison", n=len(rows), key=obj["key"], label=_t(f"objective.{obj['key']}")))
    for r in rows:
        sc = r["simulated_cost"]
        if sc:
            rng = f"{sc['lo']:,.1f}-{sc['hi']:,.1f}" if sc["lo"] is not None and sc["hi"] is not None else "n/a"
            sim_txt = f"{_t('cli.simulated')} {sc['mean']:,.1f} c ({_t('cli.simRange')} {rng})"
        else:
            sim_txt = f"{_t('cli.simulated')}: -"
        flags = f"  [{'; '.join(r['flags'])}]" if r["flags"] else ""
        print(f"{r['rank']:>3}. {r['name']}{flags}")
        if r.get("cost_range"):
            print(f"       {_t('cli.range')} {r['cost_range']['text']}")
        else:
            print(f"       {_t('cli.fast')} {r['cost']:,.1f} c | {sim_txt} | {_t('cli.badLuckShort')} "
                  f"{r['bad_luck_cost']:,.1f} c | {r['applications']:,.1f} {_t('cli.applications')}")
        print(f"       -> {_t('cli.recommended') if r['why'] == 'RECOMMENDED' else r['why']}")
    if not show_all and len(strategies) > 1:
        print(_t("cli.moreSteps"))
    for ns in result.get("not_supported", []):
        print(_t("cli.notSupportedItem", name=ns["name"], reason=ns["reason"]))
    print("\n" + _t("cli.footer", n=result["considered"], s=result["elapsed_s"], source=result["data"].get("source"),
                    at=result["data"].get("fetched_at")))
    unsupported = [f["name"] for f in result["features"] if f["status"] == "not supported"]
    print(f"{_t('cli.notSupported')}: " + "; ".join(unsupported))
    for f in result["features"]:            # per mechanic, with the missing piece (special fossils, Beastcrafting)
        if f["status"].startswith("not supported ("):
            st = _status(f["status"])
            print(f"{st[0].upper()}{st[1:]}: {f['name']} - {f.get('note', '')}")


def _print_steps(steps: list[dict]) -> None:
    """Main steps first, numbered 1, 2, 3 ...; recovery branches (conditional, optional) after them, each naming the
    main step it belongs to."""
    main = [st for st in steps if st.get("kind") != "recovery"]
    branches = [st for st in steps if st.get("kind") == "recovery"]
    if branches:
        print("\n  " + _t("cli.mainPlan"))
    for n, st in enumerate(main, 1):
        _print_step(st, f"{st.get('number') or n}.")
    if branches:
        print("\n  " + _t("cli.recoveryHeader"))
        for k, st in enumerate(branches, 1):
            _print_step(st, f"R{k} ({_t('cli.forStep', n=st['branch_of'])}):")


def _print_step(st: dict, label: str) -> None:
    flag = "" if st["status"] == "computed" else f" [{_status(st['status'])}]"
    print(f"\n  {label} {st['title']}{flag}")
    print(f"     {st['action']}")
    if st.get("detail"):
        print(f"     ({st['detail']})")
    if st.get("probability_text"):
        att = f", ~{st['attempts']:,.1f} {_t('cli.tries')}" if st.get("attempts") else ""
        print(f"     {_t('cli.chance')}: {st['probability_text']}{att}")
    if st.get("cost"):
        print(f"     {_t('cli.expectedCost')}: {st['cost']:,.1f} {_t('cli.chaos')}")
    print(f"     -> {st['expected_state']}")
    for f in st["on_fail"]:
        print(f"     {_t('cli.onFail')}: {f['text']}" + ("" if f["status"] == "computed" else f" [{_status(f['status'])}]"))


def _print_recovery(r: dict) -> None:
    item = r["item"]
    _print_markers(r)
    print("\n" + _t("cli.recovery", base=_g(item, "base"), cls=_g(item, "item_class"), ilvl=item["ilvl"]))
    print(f"{_t('cli.itemNow')}: " + "; ".join(f"[{m['affix']}] {_g(m, 'text')}" for m in r["have"]))
    print(f"{_t('cli.state')}:    " + r["state"])
    for w in r["warnings"]:
        print(f"! {w}")
    fp = r["fresh_plan"]
    sim = fp.get("simulation")
    if sim and "n" in sim:
        check = _t("cli.rec.check", a=f"{sim['analytic'] * 100:.4g}", s=f"{sim['p'] * 100:.4g}", hits=sim["hits"],
                   n=sim["n"])
    elif sim:
        check = _t("cli.rec.per_stage")
    else:
        check = _t("cli.rec.not_simulated")
    basis = lambda b: _t(f"cli.basis.{b}")
    print(f"{_t('cli.freshPlan')}: {fp['name']} (~{fp['expected_cost']:,.1f} c, {basis(fp['basis'])}{check})")
    print(_t("cli.fromHere", c=f"{r['expected_cost_from_here']:,.1f}") + "\n")
    print(_t("cli.options", basis=basis(r["options_basis"])))
    print("  " + _t("cli.rec.header"))
    for o in r["options"]:
        fin = f"{o['cost_to_finish_this_item']:,.1f} c" if o["cost_to_finish_this_item"] is not None else "-"
        flag = "" if o["status"] == "computed" else f" [{_status(o['status'])}]"
        print(f"  {o['key']:36} {o['expected_total']:9,.1f} {o['p_progress'] * 100:8.1f}% {o['p_keep_targets'] * 100:6.1f}%"
              f" {o['p_finish_without_restart'] * 100:17.1f}%  {fin}{flag}")
    _print_steps(r["steps"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="poecraft", description="Path of Exile 1 crafting planner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--url", default=config.REPOE_BASE_URL)
    f.add_argument("--from-dir", type=Path)
    sub.add_parser("build")
    u = sub.add_parser("update")
    u.add_argument("--url", default=config.REPOE_BASE_URL)
    u.add_argument("--expect-version", help="refuse exports from another game version")
    u.add_argument("--dry-run", action="store_true", help="check source version without changing data")
    tp = sub.add_parser("templates", help="list saved templates")
    tp.add_argument("--delete", metavar="NAME")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=config.PORT)
    s.add_argument("--host", default=config.HOST)
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--production", action="store_true", default=config.PRODUCTION,
                   help="public hosting: rate limits, request size limits, security headers, access log, background "
                        "price refresh, no browser (also POECRAFT_ENV=production)")
    tj = sub.add_parser("timeless", help="Timeless Jewel data (Path of Building's tables, the passive tree)")
    tj.add_argument("action", choices=["update"])
    pr = sub.add_parser("prices")
    pr.add_argument("action", choices=["update", "show"])
    pr.add_argument("--league", help="poe.ninja league id (default: current challenge league)")
    b = sub.add_parser("bases")
    b.add_argument("query", nargs="?", default="")
    m = sub.add_parser("mods")
    m.add_argument("--base", required=True)
    m.add_argument("--ilvl", type=int, default=86)
    m.add_argument("--influence", action="append", default=[])
    m.add_argument("--tiers", action="store_true", help="also list every tier with its mod id (for plan --have)")
    p = sub.add_parser("plan")
    p.add_argument("--base", help="base item name (not needed with --paste)")
    p.add_argument("--paste", metavar="TEXT", help="item text as copied in game (Ctrl+C / Ctrl+Alt+C); English, Russian "
                   "or German. Sets base, item level, influences and targets (a literal \\n is read as a line break)")
    p.add_argument("--paste-file", metavar="PATH", help="like --paste, from a text file")
    p.add_argument("--paste-as", choices=["targets", "have"], default="targets",
                   help="use the pasted mods as targets (default) or as the mods already on the item (recovery mode; "
                        "then give the targets with --target)")
    p.add_argument("--ilvl", type=int, default=86)
    p.add_argument("--influence", action="append", default=[], help="shaper, elder, crusader, hunter, redeemer, warlord")
    p.add_argument("--target", action="append", default=[],
                   help='mod text fragment with optional threshold, e.g. "maximum Life>=90" or "suffix:Cold Resistance>=40"')
    p.add_argument("--sims", type=int, default=config.DEFAULT_SIMULATIONS)
    p.add_argument("--json", action="store_true", help="print raw JSON")
    p.add_argument("--league", help="poe.ninja league for live prices (default: current challenge league)")
    p.add_argument("--no-live-prices", action="store_true", help="skip poe.ninja, use config/prices.json only")
    p.add_argument("--have", action="append", default=[], metavar="MOD_ID",
                   help="recovery mode: mod ids already on the rare (see `mods --tiers`); repeat per mod")
    p.add_argument("--objective", choices=["min_cost", "min_attempts", "min_variance"], default="min_cost",
                   help="what 'best' means: lowest expected cost (default), fewest applications, "
                        "or lowest bad-luck cost (expected + 2 std. dev.)")
    p.add_argument("--show-all-plans", action="store_true",
                   help="also print step-by-step plans for the other simulation-checked strategies")
    p.add_argument("--lang", default="en",
                   help="output language: any poecraft/locales/<code>.json (en, ru, de). Plan explanations come from "
                        "the locale files; game data from the game's own localization (missing names stay English, [EN])")
    p.add_argument("--quiet", action="store_true", help="no progress lines on stderr")
    p.add_argument("--save-template", metavar="NAME", help="save this base, item level, influences, targets and objective "
                   "as a named template (data/templates.json)")
    p.add_argument("--load-template", metavar="NAME", help="start from a saved template (extra --target are added)")
    p.add_argument("--trade", action="store_true",
                   help="also print a search link for the official trade site (current league; asks the trade site)")
    p.add_argument("--trade-league", metavar="LEAGUE", help="league for --trade (default: the current league)")
    p.add_argument("--full-sims", action="store_true",
                   help="recovery mode: also simulate the fresh 'start over' plan (slower; its cost is calculated either way)")
    args = ap.parse_args(argv)

    if args.cmd == "update" and not config.os.environ.get("POECRAFT_STAGED_UPDATE"):
        from .data.update import update
        update(args.url, args.expect_version, args.dry_run)
        return 0

    if args.cmd in ("fetch", "update"):
        from .data import fetch
        if getattr(args, "from_dir", None):
            man = fetch.import_dir(args.from_dir)
        else:
            print(f"Downloading RePoE export from {args.url}")
            man = fetch.fetch(args.url)
        print(f"Raw data saved ({man['fetched_at']}).")
        from .data import locales
        for code, language in config.LOCALE_LANGUAGES.items():       # game-data localization exports
            try:
                if getattr(args, "from_dir", None):
                    if locales.import_language(args.from_dir, language) is None:
                        print(f"  {language}: no {args.from_dir / language} - game data stays English for '{code}'")
                else:
                    locales.fetch_language(language, args.url)
            except OSError as e:
                print(f"  {language}: download failed ({e}) - the previous locale file (if any) is kept")
    if args.cmd in ("build", "update"):
        from .data import build
        stats = build.build()
        print(f"Database built at {config.DB_PATH}: {stats}")
        from .data import locales
        for code, language in config.LOCALE_LANGUAGES.items():
            if not (config.RAW_DIR / language / "mods.json").exists():
                print(f"Game-data locale '{code}': no raw {language} export - skipped (English with the EN mark)")
                continue
            try:
                meta = locales.build(code, language)
                print(f"Game-data locale '{code}' ({language}): {meta['base_items']} base items, {meta['mods']} mod "
                      f"texts, {len(meta['excluded_incomplete_mod_texts'])} incomplete mod texts left English -> "
                      f"{locales.locale_path(code)}")
            except locales.LocaleMismatch as e:
                print(f"Game-data locale '{code}': {e} - the previous file (if any) is kept")
    if args.cmd == "update" or args.cmd == "timeless":
        from .data import timeless
        print("Downloading Timeless Jewel data (Path of Building's tables, the passive tree)...")
        try:
            man = timeless.update()
            from .data.timeless_status import status as timeless_status
            print("Timeless compatibility:", json.dumps(timeless_status(), ensure_ascii=False))
            print(f"Timeless Jewel data saved to {timeless.TIMELESS_DIR}: {man['sockets']} sockets, "
                  f"{man['tree_nodes']} passives, {len(man['tables'])} jewels")
        except (OSError, ValueError) as e:
            print(f"Timeless Jewel data: download failed ({e}) - the previous data (if any) is kept")
            if args.cmd == "timeless":
                return 1
    if args.cmd in ("fetch", "build", "update", "timeless"):
        return 0

    if args.cmd == "prices":
        from .data import ninja
        from .engine.prices import Prices
        if args.action == "update":
            print("Fetching prices from poe.ninja (cached responses are reused for 5-30 min)...")
            status = ninja.update(args.league)
        status = Prices().status_json()
        if status["state"] == "live":
            print(f"Live prices: {status['league']}, fetched {status['fetched_at']}, {status['live_count']} items")
            for cat, c in status.get("categories", {}).items():
                print(f"  {cat:10} " + (f"{c['count']} items" + (" (cached)" if c.get("cached") else "")
                                        if c["ok"] else f"FAILED: {c['error']}"))
        if status.get("message"):
            print(f"! {status['message']}")
        for kp in status["key_prices"]:
            print(f"  {kp['name']:30} {kp['chaos']:>10.3f} c   [{kp['source']}]")
        return 0

    if args.cmd == "serve":
        from .server import serve
        serve(args.host, args.port, open_browser=not args.no_browser, production=args.production)
        return 0

    if args.cmd == "templates":
        from . import templates
        if args.delete:
            print(_t("cli.tpl.deleted" if templates.delete(args.delete) else "cli.tpl.missing", name=args.delete))
            return 0
        saved = templates.load_all()
        for name, t in sorted(saved.items()):
            print(f"{name:28} {t.get('base', '?'):28} ilvl {t.get('ilvl')}  targets {len(t.get('targets', []))}"
                  f"  ({t.get('saved_at', '')})")
        if not saved:
            print(_t("cli.tpl.none", path=templates.path()))
        return 0

    from .data.repository import get_data, get_rules
    data = get_data()
    if args.cmd == "bases":
        for base in data.find_bases(args.query, limit=40):
            print(f"{base.name:32} {base.item_class:26} drop lvl {base.drop_level}")
        return 0

    from .engine.context import ItemContext
    tpl = None
    if args.cmd == "plan" and args.load_template:
        from . import templates
        tpl = templates.get(args.load_template)
        if tpl is None:
            print(f"{_t('cli.error')}: " + _t("cli.tpl.missing", name=args.load_template))
            return 2
        args.ilvl = tpl.get("ilvl", args.ilvl)
        args.influence = sorted(set(args.influence) | set(tpl.get("influences", [])))
        args.objective = tpl.get("objective", args.objective)
    pasted = _import_paste(args, data) if args.cmd == "plan" else None
    if args.cmd == "plan" and pasted is None and tpl is None and not args.base:
        print(f"{_t('cli.error')}: --base / --paste / --load-template")
        return 2
    if pasted is not None and not pasted.get("base_id"):
        return 2
    if pasted:
        base = data.bases[pasted["base_id"]]
    elif tpl and not args.base:
        base = data.bases.get(tpl.get("base_id")) or data.base_by_name(tpl["base"])
    else:
        base = data.base_by_name(args.base)
    ctx = ItemContext(data, get_rules(), base, args.ilvl, args.influence)
    if args.cmd == "mods":
        for fam in sorted(ctx.families().values(), key=lambda f: (f.affix, f.label)):
            j = fam.to_json()
            print(f"[{fam.affix:6}] {fam.label:60} max {j['max_value']}  ({', '.join(sorted(fam.sources))})")
            if args.tiers:
                for t in fam.tiers:
                    print(f"           {t['mod_id']:42} {t['label']:28} {t['text']}")
        return 0

    from .engine.planner import Planner
    from .engine.prices import Prices
    global LANG
    LANG = args.lang
    specs = _targets_from_cli(ctx, args.target)
    if pasted and args.paste_as == "targets":
        specs = [_spec_from_import(t) for t in pasted["targets"]] + specs
    if tpl:
        specs = [dict(t) for t in tpl.get("targets", [])] + specs
    if not specs:
        print(f"{_t('cli.error')}: --target")
        return 2
    if args.save_template:
        from . import templates
        where = templates.save(args.save_template, {"base": base.name, "base_id": base.id, "ilvl": args.ilvl,
                                                    "influences": list(args.influence), "targets": specs,
                                                    "objective": args.objective})
        print(_t("cli.tpl.saved", name=args.save_template, path=where))
    if not args.no_live_prices:
        from .data import ninja
        ninja.update(args.league)   # no-op while the cache is fresh; never raises
    prices = Prices(use_live=not args.no_live_prices)
    if args.have:
        progress = None if args.quiet else _progress_printer()
        raw = Planner(prices=prices, sims=args.sims, progress=progress).recover(
            base.id, args.ilvl, args.influence, specs, args.have, full_sims=args.full_sims, lang=None)
        result = _present(raw)
        if args.json:
            json.dump(result, sys.stdout, indent=2, default=str, ensure_ascii=False)
        else:
            _print_recovery(result)
        return 0
    progress = None if args.quiet else _progress_printer()
    raw = Planner(prices=prices, sims=args.sims, progress=progress).plan(base.id, args.ilvl, args.influence, specs,
                                                                         objective=args.objective, lang=None)
    result = _present(raw)
    if args.json:
        json.dump(result, sys.stdout, indent=2, default=str, ensure_ascii=False)
    else:
        _print_plan(result, show_all=args.show_all_plans)
    if args.trade:
        _print_trade(base, args, specs)
    return 0


def _print_trade(base, args, specs) -> None:
    from .engine.tradelink import trade_search
    from .messages import render
    r = render(trade_search(base.id, args.ilvl, args.influence, specs, league=args.trade_league), LANG)
    print("\n" + _t("cli.trade", league=r["league"], source=_t(f"cli.trade.src.{r['league_source']}")))
    print(f"  {r['url']}")
    for w in r["warnings"]:
        print(f"  ! {w}")


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, FileNotFoundError) as e:
        from .messages import Text
        msg = e.render(LANG) if hasattr(e, "render_report") else (
            e.args[0].render(LANG) if e.args and isinstance(e.args[0], Text) else str(e))
        print(f"{_t('cli.error')}: {msg}", file=sys.stderr)
        raise SystemExit(2)
