"""Crafting planner: enumerate strategy families, score them, and expand the best ones into steps.

Strategy families
-----------------
* reroll   - Chaos / Essence / Fossil / Harvest reroll until every RNG target is present
             (and, with a bench finish, the bench slot is free).
* alt      - Transmute -> Alteration(+Augment) for up to 1 prefix + 1 suffix target,
             then Regal + Exalted slams for the rest (abort & scour on a miss).
* +rec     - the same start, but instead of rerolling/scouring on a miss the item follows the
             optimal recovery policy (Exalt, Orb of Annulment, Prefixes/Suffixes Cannot Be Changed
             + Annul/Scour/Chaos, Cannot Roll Attack/Caster + Exalt) computed by RecoveryMDP.
Each family can be combined with one crafting-bench finish, or with two bench crafts via
Multimod (reroll families), and with a Divine Orb estimate for value thresholds.

Expected cost model: every attempt is independent, a failed attempt restarts the loop,
so E[cost] = E[cost of one attempt] / P(success of one attempt). Recovery variants solve
the same fixed point over item states (analytic.RecoveryMDP).

Planner.recover() answers "I already have this item - what now?" with the same MDP.
"""
from __future__ import annotations

import dataclasses
import itertools
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .. import config
from ..messages import CUR, G, GL, J, M, N, Text, render
from ..messages import P as _Pct
from ..data.repository import BenchCraft, Fossil, GameData, Mod, get_data, get_rules
from ..data.eldritch import SIDE_NAMES, get_eldritch
from ..data.elevated import get_elevated
from ..data.special import get_special
from ..data.veiled import get_veiled
from . import eldritch, influence, special, veiled
from .analytic import RESTART, GroupedPool, MdpTooLarge, RecoveryMDP
from .context import ItemContext, PoolEntry, Target, event_only, generic_text, to_stored
from .prices import Prices
from .progress import Progress
from .simulate import Simulator, satisfies, satisfies_multi, scour_side, wilson

COMPUTED, APPROX, NOT_COMPUTED = "computed", "approximate", "not computed"

# Short names of recovery actions (message "m.short.<action key>"); bench metacraft names come from the game data
_SHORT_KEYS = ("slam", "annul", "slam_no_attack", "slam_no_caster", "lock_annul_prefix", "lock_annul_suffix",
               "lock_scour_prefix", "lock_scour_suffix", "lock_chaos_prefix", "lock_chaos_suffix", "restart")


def _short(key: str):
    """Short label of a recovery action; unknown keys are shown as they are."""
    if key not in _SHORT_KEYS:
        return key
    mc = get_rules()["metacrafts"]
    meta = lambda rule, english: G("mod", mc.get(rule), english)
    return M(f"m.short.{key}", exalt=CUR("Exalted Orb"), annul=CUR("Orb of Annulment"), scour=CUR("Orb of Scouring"),
             chaos=CUR("Chaos Orb"), meta_p=meta("prefixes_locked", "Prefixes Cannot Be Changed"),
             meta_s=meta("suffixes_locked", "Suffixes Cannot Be Changed"),
             no_attack=meta("no_attack", "Cannot roll Attack Modifiers"),
             no_caster=meta("no_caster", "Cannot roll Caster Modifiers"))


def _currency_params() -> dict:
    """Currency names a rules text may mention, as game-data parameters (each language uses the ones it needs)."""
    names = {"scour": "Orb of Scouring", "annul": "Orb of Annulment", "exalt": "Exalted Orb", "chaos": "Chaos Orb",
             "divine": "Divine Orb", "alt": "Orb of Alteration", "trans": "Orb of Transmutation",
             "aug": "Orb of Augmentation", "regal": "Regal Orb", "alch": "Orb of Alchemy",
             "fracturing": "Fracturing Orb", "conflict": "Orb of Conflict", "dominance": "Orb of Dominance",
             "awakener": "Awakener's Orb", "veiled_chaos": "Veiled Chaos Orb", "veiled_exalt": "Veiled Exalted Orb"}
    return {k: CUR(v) for k, v in names.items()}


class _FeatureText(Text):
    """A feature-card text: message "m.feature.<id>.<field>", or the mechanics.json English when no key exists
    (marked EN outside English, never shown as a raw key)."""
    __slots__ = ("key", "english")

    def __init__(self, key: str, english: str):
        self.key, self.english = key, english

    def render(self, lang: str) -> str:
        from .. import i18n
        if i18n.lookup("en", self.key)[1]:          # no English template: fall back to the rules text
            return self.english if lang == "en" else i18n.FALLBACK_MARK + self.english
        return M(self.key, **_currency_params()).render(lang)


def feature_rows(rules: dict) -> list[dict]:
    """The rules feature list with translatable name / note (status stays a fixed string)."""
    out = []
    for f in rules["features"]:
        row = {"status": f["status"], "name": _FeatureText(f"m.feature.{f.get('id')}.name", f["name"])}
        if f.get("note"):
            row["note"] = _FeatureText(f"m.feature.{f.get('id')}.note", f["note"])
        out.append(row)
    return out


def rule_note(name: str) -> Text:
    """A rules note (poecraft/rules/mechanics.json `name`) as message "m.rule.<name>", with every currency it may
    name passed as a game-data parameter (each language's template uses the ones it needs)."""
    return M(f"m.rule.{name}", **_currency_params())


_LOCK_CHAOS_NOTE = M("m.note.lock_chaos", chaos=CUR("Chaos Orb"))
_BENCH_INFERRED_NOTE = M("m.note.bench_inferred")
_BASE_PRICE_NOTE = M("m.note.base_price")
_AISLING_REMOVE_NOTE = M("m.note.aisling_remove")
_LOWER_ESSENCE_NOTE = M("m.note.lower_essence")
_ESSENCE_ANNUL_NOTE = M("m.note.essence_annul", annul=CUR("Orb of Annulment"))


def _mask(targets) -> int:
    return sum(1 << t.index for t in targets)


def _shown(spec: dict) -> float:
    """A spec's threshold as typed / displayed (Planner._specs_to_stored keeps it next to the stored min)."""
    return float(spec["min_shown"] if spec.get("min_shown") is not None else spec["min"])


def _fmt_p(p: float) -> Text:
    return _Pct(p)


# What "best" means. Every objective scores every strategy variant with the fast calculation; metrics include
# recovery branches (Annul / metacraft spending is part of the expected cost, its spread and its applications).
OBJECTIVES = {
    "min_cost": {
        "label": M("m.obj.min_cost.label"),
        "description": M("m.obj.min_cost.desc"),
        "unit": M("m.unit.c"),
        "score": lambda m: m["cost"],
    },
    "min_attempts": {
        "label": M("m.obj.min_attempts.label"),
        "description": M("m.obj.min_attempts.desc"),
        "unit": M("m.unit.applications"),
        "score": lambda m: m["apps"],
    },
    "min_variance": {
        "label": M("m.obj.min_variance.label"),
        "description": M("m.obj.min_variance.desc"),
        "unit": M("m.unit.c"),
        "score": lambda m: m["risk"],
    },
}


class Infeasible(ValueError):
    """No supported strategy reaches the targets. `report` says what is reachable, what is not, and why."""

    def __init__(self, report: dict):
        self.raw_report = report                     # language-free (message objects): render_report(lang)
        self.report = render(report, "en")           # English, plain strings (as before)
        super().__init__(self.render("en"))

    def render_report(self, lang: str) -> dict:
        return render(self.raw_report, lang)

    def message(self) -> Text:
        rep = self.raw_report
        lines = [M("m.inf.head", base=rep["base"])]
        lines += [M("m.inf.unreachable", target=u["target"], reason=u["reason"]) for u in rep["unreachable"]]
        lines += [M("m.inf.conflict", text=c) for c in rep["conflicts"]]
        lines += [M("m.inf.slots", text=c) for c in rep["slot_problems"]]
        if rep.get("extra"):
            lines.append(M("m.inf.extra", text=rep["extra"]))
        if rep["reachable"]:
            lines.append(M("m.inf.reachable", list=J([M("m.inf.reachable_item", target=x["target"],
                                                         sources=J(x["sources"], ", ")) for x in rep["reachable"]], "; ")))
        return J(lines, "\n")

    def render(self, lang: str) -> str:
        return self.message().render(lang)


def select_recommended(ranked: list, verify, max_checks: int = 16):
    """Pick the recommended candidate from `ranked` (best objective score first).

    verify(c) -> (checked, agrees, reason). Candidates are checked best-first; one whose fast calculation
    disagrees with its simulation beyond tolerance is demoted (with the reason) and the next one is tried.
    If nothing can be checked (simulation off) the top-scored candidate wins unverified.
    If every checked candidate disagrees, the top-scored one is returned unverified rather than inventing a plan.
    Returns (winner, [(demoted, reason)], verified)."""
    demoted = []
    for c in ranked[:max_checks]:
        checked, agrees, reason = verify(c)
        if not checked:
            return c, demoted, False
        if agrees:
            return c, demoted, True
        demoted.append((c, reason))
    return ranked[0], demoted, False


@dataclass
class BenchChoice:
    target: Target
    craft: BenchCraft
    mod: Mod
    cost: float           # expected cost including re-crafts for the value threshold
    value_chance: float
    kind: str = "bench"   # "influence" / "veiled": a slot reserved for the final influence Exalt / Aisling (no craft)
    influence: str | None = None

    @property
    def groups(self) -> tuple[str, ...]:
        return self.mod.groups


@dataclass
class Strategy:
    family: str
    name: str
    bench: BenchChoice | None
    rng_targets: list
    p: float                      # chance that one attempt of the main loop succeeds
    cost_rng: float
    cost_bench: float
    divines: float
    cost_divine: float
    params: dict = field(default_factory=dict)
    status: str = COMPUTED
    notes: list = field(default_factory=list)
    sim: dict | None = None
    cost_eldritch: float = 0.0    # Ember/Ichor spend for Eldritch implicit targets (0 when none are targeted)
    cost_influence: float = 0.0   # influence Exalt / Awakener's Orb spend (0 when no influence is targeted)
    cost_fracture: float = 0.0    # getting the fractured mod (reroll + Fracturing Orb, with restarts); 0 otherwise
    cost_veiled: float = 0.0      # Aisling / Annulment retries / the Alchemy before Veiled Chaos; 0 otherwise
    cost_dominance: float = 0.0   # one Orb of Dominance (best case of a range plan); 0 otherwise

    @property
    def cost(self) -> float:
        """Expected cost to an item with every target at a tier that can reach its threshold. Divine Orbs that
        reroll the numbers up to the thresholds are an optional extra (cost_divine), not part of it."""
        return (self.cost_rng + self.cost_bench + self.cost_eldritch + self.cost_influence
                + self.cost_fracture + self.cost_veiled + self.cost_dominance)

    @property
    def cost_with_divine(self) -> float:
        return self.cost + self.cost_divine


@dataclass
class ConflictPart:
    """Orb of Conflict route to a tier 2 / tier 1 Eldritch implicit (used like an EldritchOption). Its cost is
    the best case only - a lower bound; the plan carrying it is a range plan."""
    label: str
    side: str
    currency: object          # the Exceptional (tier 3) Ember / Ichor
    t3: object                # EldritchOption for the tier 3 step
    mod: object               # the target tier's EldritchMod
    other_currency: object    # Lesser currency of the other side
    other_price: float
    conflict_price: float
    target_tier: int
    raises: int               # Orbs of Conflict needed in the best case (3 - target tier)
    conflict: bool = True
    p: float = 1.0            # best case per Orb: only used for the application count

    @property
    def tier(self) -> int:
        return self.target_tier

    @property
    def cost(self) -> float:
        return self.t3.cost + self.raises * (self.other_price + self.conflict_price)

    @property
    def variance(self) -> float:
        return self.t3.variance


class Planner:
    def __init__(self, data: GameData | None = None, rules: dict | None = None,
                 prices: Prices | None = None, sims: int = config.DEFAULT_SIMULATIONS, seed: int = 1,
                 progress=None, sim_time_limit: float | None = None, sim_phase_limit: float | None = None):
        self.data = data or get_data()
        self.rules = rules or get_rules()
        self.prices = prices or Prices()
        self.sims = sims
        self.seed = seed
        # progress: a listener callable (or a Progress) told about phases and X / Y; it never changes results
        self.progress = progress if isinstance(progress, Progress) else Progress(progress)
        # wall-clock limits for the simulation check only (see config.SIM_TIME_LIMIT_S)
        self.sim_time_limit = config.SIM_TIME_LIMIT_S if sim_time_limit is None else sim_time_limit
        self.sim_phase_limit = config.SIM_PHASE_LIMIT_S if sim_phase_limit is None else sim_phase_limit
        self.rec_time_limit = config.RECOVERY_TIME_LIMIT_S
        self.rec_phase_limit = config.RECOVERY_PHASE_LIMIT_S
        self._rec_spent = 0.0                  # seconds used by recovery-policy solves in the current plan
        self._rec_skipped: list = []           # strategy names whose recovery variant ran out of time
        self._running = False
        self._sim_spent = 0.0                  # seconds of simulation used by the current plan
        self._sim_estimate = 0                 # strategies the simulation phase is expected to check
        self._deadline = float("inf")          # end of the current strategy's simulation time
        self._timeouts: list = []              # strategy names whose simulation hit the per-strategy limit
        self._sim_skipped: list = []           # strategy names not simulated: phase limit used up
        self._gref: dict = {}                  # stat line -> {english label: (mod id, english text)}
        self._tsuffix: dict = {}               # (stat line, target index) -> translated label suffix
        self.rare_n = {int(k): v for k, v in self.rules["rare_mod_count"].items()}
        self.magic_n = {int(k): v for k, v in self.rules["magic_mod_count"].items()}

    # ================================================================== entry
    def plan(self, base_id: str, ilvl: int, influences, target_specs: list[dict],
             objective: str = "min_cost", lang: str | None = "en") -> dict:
        """Score every strategy variant under `objective`, simulation-check candidates best-first and
        recommend the best one whose fast calculation the simulation confirms.
        Raises Infeasible (a ValueError) with a structured report when no strategy can reach the targets.
        Explanations are message objects until rendered: `lang` renders the result into that language;
        lang=None returns it language-free (messages.render(result, lang) later, e.g. on a language switch)."""
        with self._run():
            result = self._plan(base_id, ilvl, influences, target_specs, objective)
        return result if lang is None else render(result, lang)

    def _run(self):
        """Per-run bookkeeping (timeouts, progress). Nested runs (recover -> plan) share the outer one."""
        import contextlib

        @contextlib.contextmanager
        def run():
            if self._running:
                yield
                return
            self._running = True
            self._sim_spent, self._timeouts, self._sim_skipped = 0.0, [], []
            self._rec_spent, self._rec_skipped = 0.0, []
            self.progress.start("calculating")
            try:
                yield
            finally:
                self._running = False
                self.progress.finish()
        return run()

    def _plan(self, base_id: str, ilvl: int, influences, target_specs: list[dict], objective: str) -> dict:
        if objective not in OBJECTIVES:
            raise ValueError(M("m.err.unknown_objective", objective=objective, choices=", ".join(OBJECTIVES)))
        t0 = time.time()
        base = self.data.bases.get(base_id) or self.data.base_by_name(base_id)
        ctx = ItemContext(self.data, self.rules, base, int(ilvl), influences)
        if not target_specs:
            raise ValueError(M("m.err.no_targets"))
        target_specs = self._specs_to_stored(ctx, target_specs)
        exp_specs = [s for s in target_specs if not s.get("eldritch")]
        eld_specs = [s for s in target_specs if s.get("eldritch")]
        veil_specs = [s for s in exp_specs if s.get("veiled")]
        exp_specs = [s for s in exp_specs if not s.get("veiled")]
        el_specs = [s for s in exp_specs if s.get("elevated")]
        exp_specs = [s for s in exp_specs if not s.get("elevated")]
        fos_specs = [s for s in exp_specs if s.get("fossil")]
        bc_specs = [s for s in exp_specs if s.get("beastcraft")]
        exp_specs = [s for s in exp_specs if not (s.get("fossil") or s.get("beastcraft"))]
        inf_specs = [s for s in exp_specs if s.get("influence")]
        exp_specs = [s for s in exp_specs if not s.get("influence")]
        for s in list(inf_specs):        # the base already has that influence: an ordinary explicit target
            if s["influence"] in ctx.influences:
                inf_specs.remove(s)
                exp_specs.append({k: v for k, v in s.items() if k != "influence"})
        self.prices.missing.clear()
        self.prices.tier_default_essences.clear()
        eld_parts, eld_report = self._eldritch_parts(ctx, eld_specs)
        inf_items, inf_report = self._influence_targets(ctx, inf_specs, exp_specs, eld_specs)
        frac_report = self._fracture_report(ctx, exp_specs, inf_specs)
        veil_report, veil_t = self._veiled_report(ctx, veil_specs, exp_specs, inf_specs)
        dom_report, dom = self._dominance_report(ctx, el_specs, exp_specs, inf_specs, veil_specs)
        sp_report, sf = self._special_report(ctx, fos_specs, bc_specs, exp_specs, bool(
            inf_specs or veil_specs or el_specs or any(s.get("fractured") for s in exp_specs)))
        report = self._diagnose(ctx, exp_specs, eld_report, inf_report, frac_report, veil_report, dom_report,
                                sp_report)
        if report is not None:
            raise Infeasible(report)
        targets = ctx.make_targets(exp_specs)
        self._register_mods([e.mod for e in ctx.base_entries] + [m for _, m in ctx.essence_options]
                            + [m for _, m in ctx.bench_options])
        warnings: list = []

        base_pool = ctx.pool()
        info = []
        for t in targets:
            w = sum(e.weight for e in base_pool if t.satisfied_by(e.mod))
            ess = [(e, m) for e, m in ctx.essence_options if t.satisfied_by(m)]
            bench = self._bench_choice(ctx, t)
            info.append({"roll_weight": w, "essences": ess, "bench": bench})
            if w <= 0 and not ess and not bench:
                raise ValueError(M("m.err.unobtainable", target=self._td(t), base=G("base", base.id, base.name), ilvl=ilvl))
        total_w = sum(e.weight for e in base_pool)
        over = [(e, m) for e, m in ctx.essences_over_ilvl if any(t.satisfied_by(m) for t in targets)]
        if over:                 # say why the cheap essences for these targets are missing
            warnings.append(M("m.warn.essence_ilvl", ilvl=ilvl, names=J(
                [M("m.ess.limit", name=G("base", e.id, e.name), lvl=e.item_level_restriction) for e, _ in over], ", ")))

        frac_i = next((i for i, sp in enumerate(exp_specs) if sp.get("fractured")), None)
        if sf is not None:                           # a special-fossil target: special fossil into target
            strategies = self._special_fossil_strategies(ctx, targets, info, base_pool, sf)
        elif dom is not None:                        # an Elevated target: Orb of Dominance (range plan)
            strategies = self._dominance_strategies(ctx, targets, info, base_pool, exp_specs, dom)
        elif veil_t is not None:                     # a '(Veiled)' target: Aisling or Veiled Chaos, then unveil
            strategies = self._veiled_strategies(ctx, targets, info, base_pool, exp_specs, veil_t)
        elif frac_i is not None:                       # the user asked for this mod to be the fractured one
            strategies = self._fracture_strategies(ctx, targets, info, base_pool, targets[frac_i], chosen=False)
        elif len(inf_items) == 1:
            strategies = self._conqueror_strategies(ctx, targets, info, base_pool, exp_specs, inf_items[0])
        elif len(inf_items) == 2:
            strategies = self._awakener_strategies(ctx, targets, base_pool, inf_items)
        else:
            strategies = self._explicit_strategies(ctx, targets, info, base_pool, exp_specs) if targets else []
            if len(targets) >= 2 and not ctx.influences:
                # "fracture a useful mod, then continue": the planner fractures the rarest rollable target first
                rollable = [t for t in targets if info[t.index]["roll_weight"] > 0 or info[t.index]["essences"]]
                if rollable:
                    hardest = min(rollable, key=lambda t: info[t.index]["roll_weight"] or float("inf"))
                    strategies += self._fracture_strategies(ctx, targets, info, base_pool, hardest, chosen=True)
        if not strategies and not eld_parts:
            raise Infeasible(self._report(ctx, exp_specs, [], [], [], extra=M("m.inf.together")))
        pure: list[Strategy] = []          # explicit-only variants that do not reach an Eldritch target
        if eld_parts:
            if strategies:
                pure, strategies = strategies, self._with_eldritch(strategies, eld_parts)
            else:
                strategies = [self._eldritch_only(eld_parts)]
        # ---- selection: score everything with the fast calculation, verify best-first by simulation
        obj = OBJECTIVES[objective]
        for s in strategies + pure:
            s.params["metrics"] = self._metrics(s)
        pure.sort(key=lambda s: (obj["score"](s.params["metrics"]), s.cost))
        # the simulation phase checks the recommended plan and the best variant of every family (plus any demoted)
        self._sim_estimate = len({s.family for s in strategies if s.family not in ("eldritch", "fracture")}) + 1

        def verify(s: Strategy):
            if s.family in ("eldritch", "fracture"):
                return False, True, "closed form"      # calculated odds only: nothing to simulate
            if s.family == "awakener" and not s.rng_targets:
                return False, True, "nothing random"   # no explicit targets left for the reforge to hit
            if self.sims <= 0:
                return False, True, "simulation is off"
            if s.sim is None and not self._sim_budget_left(s):
                return False, True, "simulation time limit"
            if s.sim is None:
                self._simulate(ctx, targets, s)
            if s.sim and s.sim.get("disagrees"):
                return True, False, self._disagree_text(s)
            return True, True, ""

        best, demoted, verified, schematic = self._select(strategies, verify, obj)
        return self._finish_plan(t0, ctx, base, targets, info, total_w, base_pool, strategies, pure, best, demoted,
                                 verified, verify, obj, objective, warnings, eld_parts, inf_items, veil_t, dom,
                                 schematic, sf)

    def _explicit_strategies(self, ctx, targets, info, base_pool, exp_specs) -> list[Strategy]:
        """Every explicit strategy variant (reroll / alt families, bench and multimod finishes, recovery)."""
        strategies: list[Strategy] = []
        bench_options = [None] + [i["bench"] for i in info if i["bench"]]
        benchables = [i["bench"] for i in info if i["bench"] and i["bench"].kind == "bench"]   # real crafts only
        pairs = list(itertools.combinations(benchables, 2))
        self.progress.grow(len(bench_options) * 5 + len(pairs))
        for bench in bench_options:
            rng = [t for t in targets if bench is None or t.index != bench.target.index]
            for family in (lambda: self._chaos(ctx, targets, rng, bench, base_pool),
                           lambda: self._essences(ctx, targets, rng, bench, info),
                           lambda: self._fossils(ctx, targets, rng, bench),
                           lambda: self._harvest(ctx, targets, rng, bench, base_pool),
                           lambda: self._alt_regal(ctx, targets, rng, bench, base_pool)):
                strategies += family()
                self.progress.step()
        combos = []
        for s in sorted((s for s in strategies if s.family == "fossil"), key=lambda s: s.cost):
            if s.params["fossils"] not in combos:
                combos.append(s.params["fossils"])
        for pair in pairs:
            rng = [t for t in targets if t.index not in (pair[0].target.index, pair[1].target.index)]
            strategies += self._multimod(ctx, targets, rng, pair, info, base_pool, combos[:3])
            self.progress.step()
        strategies = [s for s in strategies if s.p > 0 and s.cost < float("inf")]
        if not strategies:
            raise Infeasible(self._report(ctx, exp_specs, [], [], [], extra=M("m.inf.no_combo")))
        strategies.sort(key=lambda s: s.cost)

        # Annul / metacraft recovery variants of the best strategy of every family
        best_per: dict[str, Strategy] = {}
        for s in strategies:
            best_per.setdefault(s.family, s)
        self.progress.grow(len(best_per))
        for s in list(best_per.values()):
            r = self._recovery_variant(ctx, targets, s)
            if r is not None:
                strategies.append(r)
                s.params["recovery_ref"] = r
            self.progress.step()
        return strategies

    def _finish_plan(self, t0, ctx, base, targets, info, total_w, base_pool, strategies, pure, best, demoted,
                     verified, verify, obj, objective, warnings, eld_parts, inf_items=(), veil_t=None, dom=None,
                     schematic=False, sf=None) -> dict:
        for s, why in demoted:
            s.notes.append(M("m.note.demoted", why=why))
        # simulation-check the best variant of every family too, so the comparison table can show it
        shown = [best]
        for s in strategies:
            if s is not best and s.family not in {x.family for x in shown}:
                shown.append(s)
                verify(s)
        selection_note = self._selection_note(best, demoted, verified, obj)
        if schematic:
            selection_note = M("m.sel.schematic", mechs=J(self._range(best)["mechanics"], " / "))
        recovery_summary = None
        plain = [s for s in strategies if "mdp" not in s.params]
        plain.sort(key=lambda s: s.cost)
        if "mdp" in best.params and plain and plain[0].cost > best.cost and not self._range(best):
            prev = plain[0]
            recovery_summary = {"without": prev.name, "without_cost": prev.cost, "with": best.name,
                                "with_cost": best.cost, "p_finish_without_reroll": best.p}
            best.notes.insert(0, M("m.note.recovery_beats", prev=prev.name, prev_cost=prev.cost,
                                   cost=best.cost, p=_fmt_p(best.p)))
        if 1 / best.p > 1e6 and best.family not in ("alt", "alt+rec"):
            warnings.append(M("m.warn.million"))
        if self.prices.missing:
            warnings.append(M("m.warn.no_price", names=J([CUR(n) for n in sorted(self.prices.missing)], ", ")))
        live_msg = self.prices.live_status.get("message")
        if self.prices.live_status.get("state") != "live" or live_msg:
            st = self.prices.live_status
            warnings.insert(0, M(st["message_key"], **st.get("message_params", {})) if st.get("message_key")
                            else live_msg)
        used_essences = {s.params["essence"].name for s in shown if "essence" in s.params}
        estimated = sorted(used_essences & self.prices.tier_default_essences.keys())
        live = self.prices.live_status.get("state") in ("live", "stale")
        if estimated and not live:
            warnings.append(M("m.warn.essence_defaults", names=J([CUR(n) for n in estimated], ", ")))
        for name in estimated if live else ():
            msg = M("m.warn.essence_not_listed", name=CUR(name), league=self.prices.live_status.get("league"),
                    price=self.prices.tier_default_essences[name])
            warnings.append(msg)
            for s in shown:
                if "essence" in s.params and s.params["essence"].name == name:
                    s.notes.append(msg)
        # Embers / Ichors priced from the config's league-start estimates (live price unavailable)
        used_eldritch = sorted({o.currency.name for s in shown for o in s.params.get("eldritch", ())} |
                               {x for s in shown for o in s.params.get("eldritch", ()) if getattr(o, "conflict", False)
                                for x in (o.other_currency.name, self.rules["conflict_orb"])})
        for name in used_eldritch:
            if self.prices.source(name) != "estimated":
                continue
            msg = M("m.warn.league_estimate", name=CUR(name), price=self.prices.get(name))
            warnings.append(msg)
            for s in shown:
                if any(o.currency.name == name for o in s.params.get("eldritch", ())):
                    s.notes.append(msg)
        # Harvest reforge: poe.ninja has no reforge price, so it always comes from config/prices.json
        harvest_key = self.rules["harvest_price_key"]
        harvest_shown = [s for s in shown if s.params.get("base_family", s.family) == "harvest"]
        if harvest_shown and self.prices.source(harvest_key) != "live":
            msg = M("m.warn.config_estimate", name=CUR(harvest_key), price=self.prices.get(harvest_key))
            warnings.append(msg)
            for s in harvest_shown:
                s.notes.append(msg)
            used_eldritch = used_eldritch + [harvest_key]       # listed in the price table too
        # special fossils: poe.ninja lists fossils; without it there may be no config entry either
        for name in sorted({s.params["special_fossil"] for s in shown if "special_fossil" in s.params}):
            if self.prices.source(name) not in ("live", "config"):
                warnings.append(M("m.warn.default_price", name=CUR(name), price=self.prices.get(name)))
        # Orb of Dominance / Orb of Conflict priced from config estimates when not live
        for key, used in ((self.rules["dominance_orb"], any("dominance" in s.params for s in shown)),
                          (self.rules["conflict_orb"], self.rules["conflict_orb"] in used_eldritch)):
            if not used:
                continue
            if key not in used_eldritch:
                used_eldritch = used_eldritch + [key]
            if self.prices.source(key) != "live":
                warnings.append(M("m.warn.config_estimate", name=CUR(key), price=self.prices.get(key)))
        # Aisling (no market price anywhere) and Veiled Chaos Orb (when not live): config estimates, said so
        for key, kind in ((self.rules["aisling"], "aisling"), (self.rules["veiled_chaos"], "veiled_chaos")):
            users = [s for s in shown if s.params.get("veiled", {}).get("kind") == kind]
            if not users:
                continue
            used_eldritch = used_eldritch + [key]
            if self.prices.source(key) == "live":
                continue
            msg = M("m.warn.aisling_estimate" if kind == "aisling" else "m.warn.config_estimate", name=CUR(key),
                    price=self.prices.get(key))
            warnings.append(msg)
            for s in users:
                s.notes.append(msg)

        return {
            "item": {
                "base": base.name, "base_id": base.id, "item_class": base.item_class, "ilvl": ctx.ilvl,
                "influences": list(ctx.influences), "tags": sorted(ctx.tags),
            },
            "targets": [{
                "label": t.label, "min": t.min_value, "affix": t.affix,
                "description": J([self._td(t), M("m.target.fractured") if self._is_fractured_target(best, t)
                                  else None]),
                "roll_chance_single": info[t.index]["roll_weight"] / total_w if total_w else 0,
                "essences": [G("base", e.id, e.name) for e, _ in info[t.index]["essences"]],
                "bench": (G("mod", info[t.index]["bench"].mod.id, info[t.index]["bench"].mod.text)
                          if info[t.index]["bench"] else None),
                **self._game_ref(t.label, [e.mod for e in ctx.base_entries] + [m for _, m in ctx.essence_options]
                                 + [m for _, m in ctx.bench_options], t),
            } for t in targets] + [{
                "label": o.label, "affix": M("m.target.implicit", side=o.side), "description": o.label,
                "eldritch": o.side,
                "sources": [CUR(o.currency.name)], **self._game_ref(generic_text(o.mod.text), [o.mod]),
            } for o in eld_parts] + [{
                "label": M("m.target.influence", target=self._td(t), inf=N(spec["influence"].title())), "affix": t.affix,
                "description": M("m.target.influence_desc", target=self._td(t), inf=N(spec["influence"].title())),
                "influence": spec["influence"],
                "sources": [CUR(self.rules["influence_exalts"][spec["influence"]])] if len(inf_items) == 1 else
                [CUR(self.rules["influence_exalts"][spec["influence"]]), CUR(self.rules["awakener_orb"])],
                **self._game_ref(t.label, [e.mod for e in influence.exalt_pool(ctx_i)], t),
            } for spec, ctx_i, t in inf_items] + ([{
                "label": M("m.target.veiled", target=self._td(veil_t)), "affix": veil_t.affix, "veiled": True,
                "description": M("m.target.veiled", target=self._td(veil_t)),
                "sources": [CUR(self.rules["aisling"]), CUR(self.rules["veiled_chaos"])],
                **self._game_ref(veil_t.label, [u.mod for u in get_veiled().mods if u.member is None], veil_t),
            }] if veil_t is not None else []) + ([{
                "label": dom["label"], "affix": dom["target"].affix, "elevated": dom["influence"],
                "description": dom["label"], "sources": [CUR(self.rules["dominance_orb"])],
                **self._game_ref(generic_text(dom["elevated"].text), [dom["elevated"]]),
            }] if dom is not None else []) + ([{
                "label": label, "affix": m.affix, "description": label, "special_fossil": sf["fossil"].name,
                "sources": [CUR(sf["fossil"].name)], **self._game_ref(generic_text(m.text), [m]),
            } for m, _, _, label in sf["targets"]] if sf is not None else []),
            "schematic": schematic,
            "not_supported": [],
            "objective": {"key": objective, "label": obj["label"], "description": obj["description"]},
            "recommended": self._display_name(best),
            "selection_note": selection_note,
            "cost_note": _BASE_PRICE_NOTE,
            "strategies": [self._strategy_json(ctx, targets, s, base_pool) for s in shown],
            "comparison": self._comparison(strategies, best, demoted, obj, pure),
            "recovery_summary": recovery_summary,
            "considered": len(strategies) + len(pure),
            "warnings": warnings + self._timeout_warnings(),
            "simulation": {"runs": self.sims, "time_limit_s": self.sim_time_limit,
                           "phase_limit_s": self.sim_phase_limit, "seconds": round(self._sim_spent, 2),
                           "timed_out": list(self._timeouts), "skipped": list(self._sim_skipped),
                           "recovery_skipped": list(self._rec_skipped)},
            "prices": self.prices.status_json(used_essences, used_eldritch),
            "features": feature_rows(self.rules) + special.feature_rows(get_special(), self.rules),
            "data": self.data.meta,
            "elapsed_s": round(time.time() - t0, 2),
        }

    # ============================================================ components
    def _bench_choice(self, ctx: ItemContext, t: Target) -> BenchChoice | None:
        best = None
        for craft, m in ctx.bench_options:
            if not t.satisfied_by(m):
                continue
            vc = t.value_chance(m)
            if vc <= 0:
                continue
            one = self.prices.basket(list(craft.cost))
            # each re-craft for a better value needs "Remove crafted mods" (1 Orb of Scouring)
            cost = one / vc + self.prices.get("Orb of Scouring") * (1 / vc - 1)
            if best is None or cost < best.cost:
                best = BenchChoice(t, craft, m, cost, vc)
        return best

    def _needs_normal(self, ess) -> bool:
        """Whispering-Wailing essences only 'upgrade a normal item to rare', so a failed attempt is scoured back to
        Normal first; Screaming and up, corruption-only and the off-ladder ones (level 0, e.g. Desolation)
        'reforge a rare item' directly. Checked against every essence's currency text by a test."""
        return 0 < ess.level < self.rules["essence_reforge_rare_min_level"] and not ess.corruption_only

    def _divine(self, rng: list[Target], entries: list[PoolEntry], existing: tuple[Mod, ...] = ()):
        q = 1.0
        for t in rng:
            fixed = next((m for m in existing if t.satisfied_by(m)), None)
            if fixed is not None:
                q *= t.value_chance(fixed)
                continue
            hits = [(e.weight, t.value_chance(e.mod)) for e in entries if t.satisfied_by(e.mod)]
            tot = sum(w for w, _ in hits)
            if tot > 0:
                q *= sum(w * v for w, v in hits) / tot
        divs = (1 / q - 1) if q > 0 else float("inf")
        return q, divs

    def _finish(self, s: Strategy, entries, existing=()):
        q, divs = self._divine(s.rng_targets, entries, existing)
        s.divines = divs if divs > 1e-6 else 0.0
        s.cost_divine = s.divines * self.prices.get("Divine Orb")
        s.params["value_q"] = q
        s.cost_bench = s.bench.cost if s.bench else 0.0
        return s

    def _reroll_p(self, ctx, targets, rng, bench, entries, existing=(), restrict=None):
        gp = GroupedPool(entries, targets, existing, bench.groups if bench else ())
        final = gp.roll(self.rare_n, ctx.limits, restrict)
        return gp.success(final, _mask(rng), bench.target if bench else None, ctx.limits)

    def _craft_cost(self, mod_id: str, item_class: str | None = None) -> float | None:
        craft = next((b for b in self.data.bench_crafts if b.mod_id == mod_id and
                      (item_class is None or item_class in b.item_classes)), None)
        return self.prices.basket(list(craft.cost)) if craft else None

    def _mdp_costs(self, item_class: str | None = None) -> dict:
        P = self.prices.get
        mc = self.rules["metacrafts"]
        c = {"exalt": P("Exalted Orb"), "annul": P("Orb of Annulment"), "scour": P("Orb of Scouring"),
             "chaos": P("Chaos Orb"),
             "remove_crafted": self.prices.basket([tuple(x) for x in self.rules["remove_crafted_cost"]])}
        for key, rule in (("meta_p", "prefixes_locked"), ("meta_s", "suffixes_locked"),
                          ("no_attack", "no_attack"), ("no_caster", "no_caster")):
            v = self._craft_cost(mc[rule], item_class)
            if v is not None:
                c[key] = v
        return c

    # ------------------------------------------------------- objective metrics
    def _attempt_apps(self, s: Strategy, fam: str) -> float:
        """Currency applications in one attempt of the main loop."""
        if fam == "alt":
            return 1 + (1 + s.params["p_aug"]) / s.params["p_alt"] + 1    # Transmute + Alterations/Augments + Regal
        return 1.0 + (1.0 if s.params.get("scour") else 0.0)            # reroll (+ Scouring for low essences)

    def _alt_attempt_var(self, s: Strategy) -> float:
        """Variance of the Alteration spend per magic success (geometric number of Alterations)."""
        P = self.prices.get
        pa = s.params["p_alt"]
        return (P("Orb of Alteration") + s.params["p_aug"] * P("Orb of Augmentation")) ** 2 * (1 - pa) / pa ** 2

    @staticmethod
    def _bench_apps(s: Strategy) -> float:
        if "bench2" in s.params:
            return s.params["apps_bench"]
        if s.bench:
            return 2 / s.bench.value_chance - 1     # crafts + 'Remove crafted mods' between value re-crafts
        return 0.0

    def _metrics(self, s: Strategy) -> dict:
        """Fast-calculation metrics for every objective: expected cost, its standard deviation and the
        expected number of applications. Recovery spending is part of all three for '+rec' variants."""
        P = self.prices.get
        fam = s.params.get("base_family", s.family)
        q = s.params.get("value_q", 1.0)
        var_div = P("Divine Orb") ** 2 * (1 - q) / q ** 2 if s.divines > 0 and q > 0 else 0.0
        prefix_apps = 1.0 if fam in ("chaos", "harvest") and not s.params.get("fixed") else 0.0
        if "veiled" in s.params:
            # rounds of (explicit / reroll part) until the unveil hits: factor = expected rounds per finished item
            inner, v = s.params["inner"], s.params["veiled"]
            mi = inner.params.get("metrics") or self._metrics(inner)
            f = v["factor"]
            x_mean = inner.cost_rng + v["extra"] / f
            var_rng = mi["var_rng"] * f + (f * f - f) * x_mean ** 2
            apps_rng, prefix_apps = mi["apps_rng"] * f + v["apps"], 0.0
        elif "influence" in s.params and "inner" in s.params:
            # attempts of (explicit plan + one influence Exalt) until the Exalt hits: geometric in the Exalt's odds
            inner, i = s.params["inner"], s.params["influence"]
            mi = inner.params.get("metrics") or self._metrics(inner)
            x_mean = inner.cost_rng + i["price"]
            var_rng = mi["var_rng"] / i["p"] + (1 - i["p"]) / i["p"] ** 2 * x_mean ** 2
            apps_rng, prefix_apps = (mi["apps_rng"] + 1) / i["p"], 0.0
        elif fam == "awakener":
            # attempts of (donor + second item + Awakener's Orb) until the reforge has the other targets
            a, p = s.params["awakener"], s.p
            dm = [d.params.get("metrics") or self._metrics(d) for _, _, d in a["donors"]]
            var_x = sum(m["var_rng"] for m in dm)
            var_rng = var_x / p + (1 - p) / p ** 2 * a["per_attempt"] ** 2
            apps_rng = (sum(m["apps_rng"] for m in dm) + 1) / p
        elif fam in ("eldritch", "fracture"):     # Eldritch- or fracture-only plan: all in the parts below
            var_rng = apps_rng = 0.0
        elif "mdp" in s.params:
            mdp = s.params["mdp"]
            apps_rng = mdp.per_item(lambda a: a.apps, self._attempt_apps(s, fam), 1.0 if fam == "alt" else 0.0)
            mean, second = mdp.cost_moments(s.params["c_attempt"])
            var_rng = max(0.0, second - mean * mean)
            if fam == "alt":                      # the Alteration spend inside each attempt is random too
                var_rng += self._alt_attempt_var(s) / mdp.p_attempt
        elif fam == "alt":
            pc = s.params["p_chain"]
            per_full = s.cost_rng * pc            # cost_rng = cost of one full attempt / P(chain succeeds)
            var_rng = self._alt_attempt_var(s) / pc + (1 - pc) / pc ** 2 * per_full ** 2
            apps_rng = (1 + (1 + s.params["p_aug"]) / s.params["p_alt"] + sum(s.params["reach"]) + (1 - pc)) / pc
        else:
            p, c = s.p, s.params["per"]           # geometric number of rerolls
            var_rng = (1 - p) / p ** 2 * c * c
            apps_rng = self._attempt_apps(s, fam) / p
        # Eldritch implicit steps are independent of the explicit part: their mean, variance and count add
        eld = s.params.get("eldritch", ())
        var_eld = sum(o.variance for o in eld)
        apps_eld = sum(1 / o.p for o in eld)
        fr = s.params.get("fracture")              # phase A (Fracturing Orb) adds independently, like Eldritch
        if fr:
            var_eld += fr["var"]
            apps_eld += fr["apps"]
        # Ranking uses the cost of reaching the thresholds the user typed, so it includes the optional Divines:
        # otherwise a cheap tier that rarely meets the threshold would beat one that always does. The plan's
        # headline numbers (*_base) leave them out; the Divines are shown as an optional extra.
        sd = math.sqrt(var_rng + var_div + var_eld)
        sd_base = math.sqrt(var_rng + var_eld)
        bench_apps = 0.0 if (s.bench and s.bench.kind in ("influence", "veiled")) else self._bench_apps(s)
        apps_base = prefix_apps + apps_rng + bench_apps + apps_eld
        return {"cost": s.cost_with_divine, "sd": sd, "risk": s.cost_with_divine + 2 * sd,
                "apps": apps_base + s.divines,
                "cost_base": s.cost, "risk_base": s.cost + 2 * sd_base, "apps_base": apps_base,
                "var_rng": var_rng, "var_div": var_div, "apps_rng": prefix_apps + apps_rng}

    def _sim_cost(self, s: Strategy) -> dict | None:
        """Total cost per finished item implied by the simulation, with a 95% range (bench/Divine as calculated)."""
        sim = s.sim
        if not sim:
            return None
        P = self.prices.get
        fam = s.params.get("base_family", s.family)
        fin = (s.cost_bench + s.cost_divine + s.cost_eldritch + s.cost_fracture   # calculated parts, not simulated
               + s.cost_dominance)
        prefix = P("Orb of Alchemy") if fam in ("chaos", "harvest") and not s.params.get("fixed") else 0.0
        infl = s.params.get("influence") if "inner" in s.params else None
        veil = s.params.get("veiled")

        def finish(r):
            """r = simulated rolling cost of one explicit plan; an influence Exalt finish repeats it until it hits."""
            if r is None:
                return None
            if veil:                              # repeated until the unveil hits, plus Aisling / retries
                return r * veil["factor"] + veil["extra"] + fin
            return (r + infl["price"]) / infl["p"] + fin if infl else r + fin

        if "mdp" in s.params:
            c = sim.get("cost_per_item")
            if not c:
                return None
            return {"mean": finish(prefix + c["mean"]), "lo": finish(prefix + c["lo"]), "hi": finish(prefix + c["hi"])}

        def total(*ps):
            if any(p <= 0 for p in ps):
                return None
            if fam == "awakener":
                return s.params["awakener"]["per_attempt"] / ps[0] + fin
            if fam == "alt":
                pa, pc = ps
                slam = sum(r * (P("Regal Orb") if j == 0 else P("Exalted Orb"))
                           for j, r in enumerate(s.params["reach"]))
                e_magic = (P("Orb of Alteration") + s.params["p_aug"] * P("Orb of Augmentation")) / pa
                return finish((P("Orb of Transmutation") + e_magic + slam + (1 - pc) * P("Orb of Scouring")) / pc)
            return finish(prefix + s.params["per"] / ps[0])

        parts = [sim] if "n" in sim else [sim["alt"], sim["chain"]]
        mean = total(*[x["p"] for x in parts])
        if mean is None:
            return None
        return {"mean": mean, "lo": total(*[x["ci"][1] for x in parts]), "hi": total(*[x["ci"][0] for x in parts])}

    @staticmethod
    def _disagree_text(s: Strategy) -> Text:
        sim = s.sim
        parts = [sim] if "n" in sim else [sim["alt"], sim["chain"]]
        x = next((x for x in parts if x.get("disagrees")), parts[0])
        txt = [M("m.disagree.p", a=x["analytic"], s=x["p"], lo=x["ci"][0], hi=x["ci"][1])]
        if sim.get("cost_simulated") and sim.get("cost_analytic"):
            txt.append(M("m.disagree.cost", a=sim["cost_analytic"], s=sim["cost_simulated"]))
        return J(txt + [M("m.disagree.tail")])

    def _selection_note(self, best: Strategy, demoted, verified: bool, obj: dict) -> Text:
        notes = [M("m.sel.demoted", name=self._display_name(d), why=why) for d, why in demoted]
        if best.family == "eldritch":
            notes.append(M("m.sel.eldritch_only"))
        elif verified:
            notes.append(M("m.sel.verified", label=obj["label"]))
        elif self.sims <= 0:
            notes.append(M("m.sel.sims_off", label=obj["label"]))
        else:
            notes.append(M("m.sel.unverified"))
        if "eldritch" in best.params and best.family != "eldritch":
            notes.append(M("m.sel.eldritch_part"))
        if "fracture" in best.params:
            notes.append(M("m.sel.fracture_after" if best.family != "fracture" else "m.sel.fracture_only",
                           orb=CUR(self.rules["fracturing_orb"])))
        if "veiled" in best.params:
            notes.append(M("m.sel.veiled_aisling" if best.params["veiled"]["kind"] == "aisling"
                           else "m.sel.veiled_side"))
        if "inner" in best.params and "influence" in best.params:
            notes.append(M("m.sel.influence"))
        if best.family == "awakener":
            notes.append(M("m.sel.awakener_reforge" if best.rng_targets else "m.sel.awakener_none",
                           orb=CUR(self.rules["awakener_orb"])))
        return J(notes, " ")

    def _comparison(self, strategies: list, best: Strategy, demoted, obj: dict, pure=()) -> list[dict]:
        """One row per evaluated strategy variant, in objective order, with why it lost. `pure` are explicit-only
        variants when Eldritch implicits are targeted: listed for comparison, never eligible."""
        dem = {id(s): why for s, why in demoted}
        win = obj["score"](best.params["metrics"])
        rows = []
        pure_ids = {id(x) for x in pure}
        for rank, s in enumerate(list(strategies) + list(pure), 1):
            m = s.params["metrics"]
            score = obj["score"](m)
            flags = []
            if s.status != COMPUTED:
                flags.append(M("m.flag.approximate"))
            if "eldritch" in s.params:
                flags.append(M("m.flag.eldritch"))
            if "fracture" in s.params:
                flags.append(M("m.flag.fracture", orb=CUR(self.rules["fracturing_orb"])))
            rg = self._range(s)
            if rg:
                flags.append(M("m.flag.range", mechs=J(rg["mechanics"], M("m.flag.range_and"))))
            if "veiled" in s.params:
                flags.append(M("m.flag.veiled_aisling" if s.params["veiled"]["kind"] == "aisling"
                               else "m.flag.veiled_side"))
            if "inner" in s.params and "influence" in s.params:
                flags.append(M("m.flag.influence"))
            if s.family == "awakener":
                flags.append(M("m.flag.awakener"))
            if s.sim:
                parts = [s.sim] if "n" in s.sim else [s.sim["alt"], s.sim["chain"]]
                low = next((x["low_confidence"] for x in parts if x.get("low_confidence")), None)
                if low:
                    flags.append(M("m.flag.low_conf", why=low))
                if not all(x["reliable"] for x in parts):
                    flags.append(M("m.flag.unverified"))
                if s.sim.get("disagrees"):
                    flags.append(M("m.flag.disagree"))
            if id(s) in pure_ids:
                flags.append(M("m.flag.no_eldritch"))
                why = M("m.why.pure")
            elif s is best and s.params.get("schematic"):
                why = M("m.why.schematic")
            elif s is best:
                why = "RECOMMENDED"                    # a marker the UI / CLI translate, not shown as is
            elif id(s) in dem:
                why = M("m.why.demoted", why=dem[id(s)])
            elif rg:
                why = (M("m.why.range_vs", low=rg["low"], best=self._range(best)["low"]) if self._range(best)
                       else M("m.why.range"))
            else:
                why = M("m.why.score", label=obj["label"], score=score, win=win, unit=obj["unit"], diff=score - win)
                if not s.sim:
                    why = J([why, M("m.why.not_checked")])
            rows.append({
                "rank": rank, "name": self._display_name(s), "family": s.family, "status": s.status,
                "score": score, "cost": m["cost"], "cost_sd": m["sd"], "bad_luck_cost": m["risk"],
                "applications": m["apps"], "simulated": s.sim is not None,
                "simulated_cost": None if rg else self._sim_cost(s), "cost_range": rg,
                "flags": flags, "why": why,
            })
        return rows

    # ------------------------------------------------------------ Eldritch implicits
    def _eldritch_parts(self, ctx: ItemContext, specs: list[dict]):
        """Cheapest Ember/Ichor option per Eldritch target, plus (reachable, unreachable, conflicts) entries for
        the feasibility report."""
        if not specs:
            return [], ((), (), ())
        data = get_eldritch()
        fams = eldritch.all_families(data)
        parts, reach, unreach, conflicts, sides = [], [], [], [], {}
        for spec in specs:
            side = spec["eldritch"]
            if side not in SIDE_NAMES:
                raise ValueError(M("m.err.eld_side", side=side))
            fam, mn = fams.get(spec.get("signature")), spec.get("min")
            if fam:
                m0 = next(iter(fam.tiers.values()))
                name = GL(m0.id, fam.label, m0.text)
            else:
                name = spec.get("label") or str(spec.get("signature"))
            label = M("m.eld.label", name=name, thr=f" (>= {_shown(spec):g})" if mn not in (None, "") else "",
                      side=N(SIDE_NAMES[side]))
            opts, why = eldritch.options(data, self.rules, ctx, spec, self.prices)
            if why and fam is not None and why.startswith("needs tier"):
                part, why2 = self._conflict_part(data, ctx, spec, fam, label)
                if part is not None:
                    reach.append({"target": label, "affix": M("m.target.implicit", side=side),
                                  "sources": [G("base", part.currency.id, part.currency.name)
                                              if hasattr(part.currency, "id") else CUR(part.currency.name),
                                              CUR(self.rules["conflict_orb"])]})
                    if side in sides:
                        conflicts.append(M("m.eld.one_per_side", a=sides[side], b=label, side=N(SIDE_NAMES[side])))
                    else:
                        sides[side] = label
                        parts.append(part)
                    continue
                why = why2 or why
            if why:
                unreach.append({"target": label, "reason": why})
                continue
            best = min(opts, key=lambda o: o.cost)
            best.label = label
            reach.append({"target": label, "affix": M("m.target.implicit", side=side),
                          "sources": [CUR(n) for n in sorted({o.currency.name for o in opts})]})
            if side in sides:
                conflicts.append(M("m.eld.one_per_side", a=sides[side], b=label, side=N(SIDE_NAMES[side])))
            else:
                sides[side] = label
                parts.append(best)
        if any(getattr(o, "conflict", False) for o in parts) and len(specs) > 1:
            conflicts.append(M("m.eld.conflict_two", orb=CUR(self.rules["conflict_orb"])))
        return parts, (reach, unreach, conflicts)

    def _with_eldritch(self, strategies: list[Strategy], parts) -> list[Strategy]:
        """Each explicit variant + the Eldritch steps. The Eldritch part is independent of explicit crafting, so
        its cost/variance/applications add; the explicit odds and their simulation are unchanged."""
        e_cost = sum(o.cost for o in parts)
        mapping, out = {}, []
        for s in strategies:
            c = dataclasses.replace(s, params=dict(s.params, eldritch=parts), notes=list(s.notes), sim=None,
                                    cost_eldritch=e_cost, status=APPROX)
            c.notes.append(self._note("eldritch_note"))
            mapping[id(s)] = c
            out.append(c)
        for c in out:
            ref = c.params.get("recovery_ref")
            if ref is not None:
                c.params["recovery_ref"] = mapping.get(id(ref), ref)
        return out

    def _eldritch_only(self, parts) -> Strategy:
        names = J([CUR(o.currency.name) for o in parts], " + ")
        return Strategy("eldritch", M("m.strategy.eldritch_only", names=names), None, [], min(o.p for o in parts),
                        0.0, 0.0, 0.0, 0.0, params={"eldritch": parts, "entries": []}, status=APPROX,
                        notes=[self._note("eldritch_note")], cost_eldritch=sum(o.cost for o in parts))

    def _eldritch_steps(self, s: Strategy) -> list[dict]:
        out = []
        for o in s.params.get("eldritch", ()):
            if getattr(o, "conflict", False):
                out += self._conflict_steps(o)
                continue
            side, cur = N(SIDE_NAMES[o.side]), CUR(o.currency.name)
            mod = G("mod", o.mod.id, o.mod.text)
            out.append({
                "title": M("m.step.eld.title", side=side),
                "action": M("m.step.eld.action", cur=cur, side=side, mod=mod),
                "detail": M("m.step.eld.detail", side=side, tier=o.tier, note=self._note("eldritch_note")),
                "probability": o.p, "probability_text": M("m.step.per", p=_fmt_p(o.p), cur=cur),
                "attempts": 1 / o.p, "cost": o.cost,
                "expected_state": M("m.step.eld.state", side=side, mod=mod),
                "on_success": M("m.step.next"),
                "on_fail": [{"text": M("m.step.eld.fail", cur=cur, side=side), "status": COMPUTED}],
                "status": APPROX,
            })
        return out

    # ------------------------------------------------------------ Fracturing Orb
    def _fracture_report(self, ctx: ItemContext, exp_specs: list[dict], inf_specs: list[dict]):
        """Reasons a 'fractured:' target cannot be planned -> ((), unreachable, conflicts, ())."""
        frac = [s for s in exp_specs if s.get("fractured")]
        if not frac:
            return (), (), (), ()
        fams = ctx.families()
        orb = CUR(self.rules["fracturing_orb"])
        unreach, conflicts = [], []
        names = []
        for spec in frac:
            fam = fams.get(spec.get("signature"))
            if fam is None:
                continue                                   # the explicit feasibility already explains it
            mn = spec.get("min")
            base_label = J([self._fam_label(fam), M("m.thr", v=float(mn)) if mn not in (None, "") else None])
            label = M("m.frac.label", label=base_label)
            names.append(label)
            why = None
            if ctx.influences:
                why = M("m.frac.influenced", orb=orb, list=J([N(i.title()) for i in ctx.influences], " + "))
            elif inf_specs:
                why = M("m.frac.with_influence", orb=orb)
            elif not ({"roll", "essence"} & fam.sources):
                why = M("m.frac.bench_only", orb=orb)
            if why:
                unreach.append({"target": label, "reason": why, "base_target": base_label})
        if len(frac) > 1:
            conflicts.append(M("m.frac.only_one", orb=orb,
                               names=J(names, ", ") if names else M("m.frac.n_targets", n=len(frac))))
        return (), unreach, conflicts, ()

    @staticmethod
    def _is_fractured_target(best: Strategy, t: Target) -> bool:
        ft = best.params.get("frac_target")
        return ft is not None and ft.index == t.index and not best.params.get("chosen")

    def _fracture_phase_a(self, ctx: ItemContext, t: Target, base_pool, info_t: dict) -> dict | None:
        """Cheapest way to get target `t` onto a rare and fracture it.
        Per attempt (fresh base): reroll until `t` is present, Divine its value, then the Fracturing Orb, which hits
        `t` with chance E[1/number of modifiers] (uniform pick assumed). A miss restarts on a fresh base."""
        P = self.prices.get
        orb, div = P(self.rules["fracturing_orb"]), P("Divine Orb")
        t0 = dataclasses.replace(t, index=0)
        opts = [(M("m.strategy.chaos", chaos=CUR("Chaos Orb")), base_pool, (), P("Chaos Orb"), P("Orb of Alchemy"), 1.0)]
        for ess, m in info_t["essences"]:
            scour = self._needs_normal(ess)
            opts.append((M("m.strategy.essence", essence=G("base", ess.id, ess.name)), base_pool, (m,),
                         self.prices.essence(ess.name, ess.level) + (P("Orb of Scouring") if scour else 0.0), 0.0,
                         2.0 if scour else 1.0))
        for fs in self._fossils(ctx, [t0], [t0], None)[:2]:
            opts.append((fs.name, fs.params["entries"], (), fs.params["per"], 0.0, 1.0))
        best = None
        for name, entries, existing, per, prefix, apps_per in opts:
            gp = GroupedPool(entries, [t0], existing)
            p_hit = inv_k = 0.0
            for st, pr in gp.roll(self.rare_n, ctx.limits).items():
                k = sum(gp.counts(st))
                if st[1] & 1 and k >= self.rules["fracture_min_mods"]:
                    p_hit += pr
                    inv_k += pr / k
            if p_hit <= 0:
                continue
            f = inv_k / p_hit
            if existing:
                q, rep = t0.value_chance(existing[0]), existing[0]
            else:
                hits = [e for e in entries if t0.satisfied_by(e.mod)]
                tot = sum(e.weight for e in hits)
                q = sum(e.weight * t0.value_chance(e.mod) for e in hits) / tot if tot else 0.0
                rep = max(hits, key=lambda e: e.weight).mod
            if q <= 0:
                continue
            div_pre = 1 / q - 1
            x = prefix + per / p_hit + div_pre * div + orb          # one attempt on one base
            var_x = per ** 2 * (1 - p_hit) / p_hit ** 2 + div ** 2 * (1 - q) / q ** 2
            cand = {"name": name, "cost": x / f, "var": var_x / f + (1 - f) / f ** 2 * x ** 2,
                    "apps": ((1.0 if prefix else 0.0) + apps_per / p_hit + div_pre + 1) / f,
                    "p_hit": p_hit, "f": f, "q": q, "div_pre": div_pre, "per": per, "prefix": prefix,
                    "orb_price": orb, "mod": rep}
            if best is None or cand["cost"] < best["cost"]:
                best = cand
        return best

    def _fracture_strategies(self, ctx, targets, info, base_pool, t_f: Target, chosen: bool) -> list[Strategy]:
        """Fracture `t_f` first (phase A), then craft the other targets around it with every existing family
        (phase B, the fractured mod is fixed: rerolls, Scouring and Annulment keep it)."""
        a = self._fracture_phase_a(ctx, t_f, base_pool, info[t_f.index])
        if a is None:
            return []
        orb = CUR(self.rules["fracturing_orb"])
        fixed = (a["mod"],)
        notes = [self._note("fracture_note"), self._note("fracture_restart_note")]
        # say why lower-tier essences are missing below (_essences skips them when a fractured mod is fixed)
        lower = [ess for t in targets if t.index != t_f.index for ess, _ in info[t.index]["essences"]
                 if self._needs_normal(ess)]
        if lower:
            notes.append(_LOWER_ESSENCE_NOTE)
        base = {"fracture": a, "frac_target": t_f, "chosen": chosen, "fixed": fixed}
        rest = [t for t in targets if t.index != t_f.index]
        if not rest:
            return [Strategy("fracture", M("m.strategy.fracture_only", orb=orb, target=self._tl(t_f)), None, [],
                             a["f"], 0.0, 0.0, 0.0, 0.0,
                             params={**base, "entries": []}, status=APPROX, notes=notes, cost_fracture=a["cost"])]
        inner: list[Strategy] = []
        benches = [None] + [info[t.index]["bench"] for t in rest
                            if info[t.index]["bench"] and info[t.index]["bench"].kind == "bench"]
        for bench in benches:
            rng = [t for t in rest if bench is None or t.index != bench.target.index]
            inner += self._chaos(ctx, targets, rng, bench, base_pool, fixed)
            inner += self._essences(ctx, targets, rng, bench, info, fixed)
            inner += self._fossils(ctx, targets, rng, bench, fixed)
            inner += self._harvest(ctx, targets, rng, bench, base_pool, fixed)
        inner = [s for s in inner if s.p > 0 and s.cost < float("inf")]
        inner.sort(key=lambda s: s.cost)
        best_per: dict[str, Strategy] = {}
        for s in inner:
            best_per.setdefault(s.family, s)
        for s in list(best_per.values()):
            r = self._recovery_variant(ctx, targets, s)
            if r is not None:
                inner.append(r)
                s.params["recovery_ref"] = r
        mapping, out = {}, []
        for s in inner:
            w = dataclasses.replace(
                s, family="fracture+" + s.family,
                name=M("m.strategy.fracture_then", orb=orb, target=self._tl(t_f), inner=s.name),
                cost_fracture=a["cost"], status=APPROX, sim=None,
                notes=list(s.notes) + notes + [self._note("fracture_kept_note")],
                params=dict(s.params, **base, base_family=s.params.get("base_family", s.family)))
            mapping[id(s)] = w
            out.append(w)
        for w in out:
            ref = w.params.get("recovery_ref")
            if ref is not None:
                w.params["recovery_ref"] = mapping.get(id(ref), ref)
        return out

    def _fracture_steps(self, s: Strategy) -> list[dict]:
        a, t = s.params["fracture"], s.params["frac_target"]
        orb = CUR(self.rules["fracturing_orb"])
        div = self.prices.get("Divine Orb")
        why = M("m.frac.chosen") if s.params.get("chosen") else ""
        detail = [M("m.frac.step1.detail")]
        if a["div_pre"] > 1e-6:
            detail.append(M("m.frac.step1.divine", divine=CUR("Divine Orb"), n=a["div_pre"]))
        return [{
            "title": M("m.frac.step1.title"),
            "action": M("m.frac.step1.action", method=a["name"], target=self._td(t), why=why),
            "detail": J(detail),
            "probability": a["p_hit"], "probability_text": M("m.per_reroll", p=_fmt_p(a["p_hit"])),
            "attempts": 1 / (a["p_hit"] * a["f"]),
            "cost": (a["prefix"] + a["per"] / a["p_hit"] + a["div_pre"] * div) / a["f"],
            "expected_state": M("m.frac.step1.state", target=self._td(t)),
            "on_success": M("m.step.next"),
            "on_fail": [{"text": M("m.frac.step1.fail", target=self._td(t)), "status": COMPUTED}],
            "status": COMPUTED,
        }, {
            "title": orb,
            "action": M("m.frac.step2.action", orb=orb, target=self._td(t)),
            "detail": M("m.frac.step2.detail", note=self._note("fracture_note")),
            "probability": a["f"], "probability_text": M("m.step.per", p=_fmt_p(a["f"]), cur=orb),
            "attempts": 1 / a["f"], "cost": a["orb_price"] / a["f"],
            "expected_state": M("m.frac.step2.state", target=self._td(t)),
            "on_success": M("m.step.next"),
            "on_fail": [{"text": M("m.frac.step2.fail_restart", target=self._td(t)), "status": COMPUTED},
                        {"text": M("m.frac.step2.fail_keep"), "status": NOT_COMPUTED}],
            "status": APPROX,
        }]

    # ------------------------------------------------------------ special fossils / Beastcrafting
    def _special_report(self, ctx: ItemContext, fos_specs, bc_specs, exp_specs, other_special: bool):
        """Feasibility of '(Faceted)' / '(Hollow)' ... and '(Beastcraft)' targets
        -> ((reachable, unreachable, conflicts, slots), dict | None)."""
        if not fos_specs and not bc_specs:
            return ((), (), (), ()), None
        data = get_special()
        reach, unreach, conflicts, slots = [], [], [], []

        def thr(spec):
            mn = spec.get("min")
            return None if mn in (None, "") else float(mn)

        beasts = {m.signature: m for m in data.beast_outcomes}
        for spec in bc_specs:
            m = beasts.get(spec.get("signature"))
            name = GL(m.id, generic_text(m.text), m.text) if m else (spec.get("label") or str(spec.get("signature")))
            mn = thr(spec)
            unreach.append({"target": M("m.sp.beast_label", name=name, thr=M("m.thr", v=_shown(spec)) if mn is not None else ""),
                            "reason": self._note("beastcraft_missing")})
        tags = set(ctx.tags)
        good, fossil = [], None
        for spec in fos_specs:
            name, mn = spec["fossil"], thr(spec)
            f = data.fossils.get(name)
            cands = [m for m in ((f.added + f.forced) if f else ()) if m.signature == spec.get("signature")]
            text = generic_text(cands[0].text) if cands else (spec.get("label") or str(spec.get("signature")))
            shown = GL(cands[0].id, text, cands[0].text) if cands else text
            label = M("m.sp.label", name=shown, thr=M("m.thr", v=_shown(spec)) if mn is not None else "",
                      short=G("base", self._fossil_id(name), name.replace(" Fossil", "")))
            fossil_name = CUR(name)
            why, pick = None, None
            if f is None:
                why = M("m.sp.no_fossil", name=name)
            else:
                status, reason, _ = special.verdict(f, self.rules)
                if status != special.SUPPORTED:
                    why = M("m.sp.status_reason", status=M(special.STATUS_TEXT.get(status, "m.status.missing_fossil")),
                            reason=reason)
                elif not cands:
                    why = M("m.sp.not_added", fossil=fossil_name,
                            list=J([GL(m.id, generic_text(m.text), m.text) for m in f.added + f.forced], "; "))
                elif f.allowed_tags and not set(f.allowed_tags) & tags:
                    why = M("m.sp.allowed_tags", fossil=fossil_name, tags=", ".join(f.allowed_tags))
                elif set(f.forbidden_tags) & tags:
                    why = M("m.sp.forbidden_tags", fossil=fossil_name, tags=", ".join(set(f.forbidden_tags) & tags))
                else:
                    t = Target(0, cands[0].signature, cands[0].stats[0].id, mn, text, cands[0].affix)
                    forced = [m for m in cands if m in f.forced]
                    spawn = forced or [m for m in cands if m.spawn_weight(tags) > 0]
                    if not spawn:
                        why = M("m.sp.no_spawn", fossil=fossil_name, base=G("base", ctx.base.id, ctx.base.name))
                    elif not [m for m in spawn if m.required_level <= ctx.ilvl]:
                        why = M("m.sp.ilvl", lvl=min(m.required_level for m in spawn))
                    elif not [m for m in spawn if m.required_level <= ctx.ilvl and t.satisfied_by(m)]:
                        why = M("m.sp.below", top=max(m.stats[0].max for m in spawn))
                    else:
                        pick = next(m for m in spawn if m.required_level <= ctx.ilvl and t.satisfied_by(m))
            if why:
                unreach.append({"target": label, "reason": why})
                continue
            reach.append({"target": label, "affix": pick.affix, "sources": [fossil_name]})
            self._register_mods([pick])
            if fossil is not None and fossil is not f:
                conflicts.append(M("m.sp.two_fossils"))
            fossil = f
            good.append((pick, text, mn, label))
        if fos_specs and other_special:
            conflicts.append(M("m.sp.with_other"))
        if good and not unreach and not conflicts:
            fams = ctx.families()
            for side in ("prefix", "suffix"):
                n = sum(1 for spec in exp_specs if spec.get("signature") in fams and fams[spec["signature"]].affix == side)
                n += sum(1 for m, *_ in good if m.affix == side)
                if n > ctx.limits[side]:
                    slots.append(M("m.sp.slots", n=n, side=side, side_word=M(f"m.side.{side}"), max=ctx.limits[side]))
        ok = bool(good) and not (unreach or conflicts or slots)
        return (reach, unreach, conflicts, slots), ({"fossil": fossil, "targets": good} if ok else None)

    def _special_fossil_strategies(self, ctx, targets, info, base_pool, sf: dict) -> list[Strategy]:
        """'Special fossil into target': the special fossil alone and with the best ordinary fossils for the other
        targets, as fossil-combo variants. Added modifiers join the pool; a forced one is guaranteed."""
        f = sf["fossil"]
        f_db = next((x for x in self.data.fossils if x.name == f.name), None)
        if f_db is None:
            return []
        n = len(targets)
        sp = [Target(n + i, m.signature, m.stats[0].id, mn, text, m.affix)
              for i, (m, text, mn, _) in enumerate(sf["targets"])]
        all_t = list(targets) + sp
        forced = tuple(m for m in f.forced)
        helpers = []
        if targets:
            for s in self._fossils(ctx, targets, list(targets), None):
                helpers += [x for x in s.params["fossils"] if x not in helpers and x is not f_db]
        combos = [(f_db,)] + [(f_db, h) for h in helpers[:2]] + ([(f_db, *helpers[:2])] if len(helpers) >= 2 else [])
        status, _, assumptions = special.verdict(f, self.rules)
        note = M("m.sp.note", fossil=CUR(f.name), desc=J([N(t) for _, t in f.descriptions], "; "),
                 assumptions=J(assumptions, " "))
        benches = [None] + [i["bench"] for i in info if i["bench"] and i["bench"].kind == "bench"]
        out = []
        for bench in benches:
            rng = [t for t in all_t if bench is None or t.index != bench.target.index]
            for combo in combos:
                pool = special.pool(ctx, combo, f, self.rules["fossil_weight_divisor"])
                p = self._reroll_p(ctx, all_t, rng, bench, pool, existing=forced)
                if p <= 0:
                    continue
                per = self._fossil_cost(combo)
                s = Strategy("fossil", self._fossil_name(combo), bench, rng, p, per / p, 0, 0, 0,
                             params={"entries": pool, "fossils": combo, "per": per,
                                     "resonator": self.rules["resonators"][str(len(combo))],
                                     "special_fossil": f.name, **({"mod": forced[0]} if forced else {})},
                             status=APPROX)
                s.notes += [self._note("fossil_note"), note]
                out.append(self._finish(s, pool, forced))
        out.sort(key=lambda s: s.cost)
        return out

    # ------------------------------------------------------------ range plans (Orb of Dominance / Orb of Conflict)
    def _range(self, s: Strategy) -> dict | None:
        """Cost range of a plan that depends on odds missing from the game data, or None for an ordinary plan.
        'low' is a true lower bound (every unknown roll goes the target's way); 'high' is None (no upper bound);
        'assumed' is a figure under a stated assumption (Dominance only), never a bound."""
        dom = s.params.get("dominance")
        conf = [o for o in s.params.get("eldritch", ()) if getattr(o, "conflict", False)]
        if not dom and not conf:
            return None
        mechs, low_r, mid_r, high_r = [], [], [], []
        mid = None
        if dom:
            orb = CUR(self.rules["dominance_orb"])
            mechs.append(orb)
            low_r.append(M("m.range.dom_low", orb=orb))
            high_r.append(M("m.range.dom_high"))
            if not conf:
                inner = s.params["inner"]
                p = dom["p_uniform"]
                mid = s.cost + (1 / p - 1) * (inner.cost_rng + inner.cost_bench + dom["price"])
                mid_r.append(M("m.range.dom_mid", n=dom["n"], p=p))
        if conf:
            orb = CUR(self.rules["conflict_orb"])
            mechs.append(orb)
            low_r.append(M("m.range.conf_low", orb=orb, needs=J([M("m.range.conf_need", n=o.raises, tier=o.target_tier)
                                                                 for o in conf], ", ")))
            high_r.append(M("m.range.conf_high"))
            mid_r.append(M("m.range.conf_mid", orb=orb))
        low = s.cost
        text = M("m.range.text", low=low, mid=M("m.range.mid", mid=mid) if mid else "")
        return {"low": low, "assumed": mid, "high": None, "mechanics": mechs, "text": text,
                "low_reason": J(low_r, "; "), "assumed_reason": J(mid_r, "; "), "high_reason": J(high_r, "; ")}

    def _select(self, strategies: list[Strategy], verify, obj: dict):
        """select_recommended over ordinary plans; a range plan is eligible only if its worst case beats the best
        ordinary plan (none can today: no upper bound). With only range plans the top one is shown as a schematic.
        Returns (best, demoted, verified, schematic)."""
        rng_of = {id(s): self._range(s) for s in strategies}
        strategies.sort(key=lambda s: (rng_of[id(s)] is not None, obj["score"](s.params["metrics"]), s.cost))
        det = [s for s in strategies if rng_of[id(s)] is None]
        ranged = [s for s in strategies if rng_of[id(s)] is not None]
        if not ranged:
            return (*select_recommended(strategies, verify), False)
        if not det:
            best = ranged[0]
            best.params["schematic"] = True
            verify(best)
            return best, [], False, True
        floor = obj["score"](det[0].params["metrics"])
        ok = [r for r in ranged if rng_of[id(r)]["high"] is not None and rng_of[id(r)]["high"] < floor]
        cands = sorted(det + ok, key=lambda s: (obj["score"](s.params["metrics"]), s.cost))
        best, demoted, verified = select_recommended(cands, verify)
        for r in ranged:
            if r not in ok:
                hi = rng_of[id(r)]["high"]
                demoted.append((r, M("m.sel.range_demoted", why=M("m.sel.range_no_upper") if hi is None
                                     else M("m.sel.range_worst", hi=hi))))
        return best, demoted, verified, False

    def _range_note(self, mech) -> Text:
        return M("m.range.note", mech=CUR(mech) if isinstance(mech, str) else mech)

    # ------------------------------------------------------------ Orb of Dominance (Elevated targets)
    def _dominance_report(self, ctx: ItemContext, el_specs, exp_specs, inf_specs, veil_specs):
        """Feasibility of Elevated targets -> ((reachable, unreachable, conflicts, slots), dict | None)."""
        if not el_specs:
            return ((), (), (), ()), None
        edata = get_elevated()
        orb = CUR(self.rules["dominance_orb"])
        reach, unreach, conflicts, slots = [], [], [], []
        good = None
        for spec in el_specs:
            inf, sig = spec["elevated"], spec.get("signature")
            mn = spec.get("min")
            mn = None if mn in (None, "") else float(mn)
            cands = [e for e in edata.mods if e.mod.signature == sig and e.influence == inf]
            name = generic_text(cands[0].mod.text) if cands else (spec.get("label") or str(sig))
            shown = GL(cands[0].mod.id, name, cands[0].mod.text) if cands else name
            label = M("m.dom.label", name=shown, thr=M("m.thr", v=_shown(spec)) if mn is not None else "", inf=N(inf.title()))
            why = None
            if not edata.mods:
                why = M("m.dom.no_pool")
            elif not cands:
                why = M("m.dom.no_mod", inf=N(inf.title()))
            elif ctx.base.item_class not in edata.classes:
                why = M("m.dom.class", orb=orb, quote=edata.orb_text[edata.orb_text.find("Can be used on"):],
                        cls=N(ctx.base.item_class))
            elif inf not in ctx.influences:
                why = M("m.dom.no_influence", inf=N(inf.title()), orb=orb)
            elif not any(e.base_type for e in cands):
                why = M("m.dom.no_link")
            else:
                t = Target(0, sig, cands[0].mod.stats[0].id, mn, name, cands[0].mod.affix)
                found = []
                tags = set(ctx.tags)
                for e in cands:
                    tiers = [m for m in self.data.mods.values() if m.type == e.base_type and m.spawn_weight(tags) > 0
                             and not event_only(m, self.rules)]
                    if tiers:
                        found.append((e, max(tiers, key=lambda m: (m.stats[0].max, m.required_level))))
                if not found:
                    why = M("m.dom.base_not_on", base=G("base", ctx.base.id, ctx.base.name), inf=N(inf.title()))
                elif not [x for x in found if x[1].required_level <= ctx.ilvl]:
                    why = M("m.dom.top_ilvl", lvl=min(x[1].required_level for x in found))
                elif not [x for x in found if t.satisfied_by(x[0].mod)]:
                    why = M("m.dom.below", top=max(x[0].mod.stats[0].max for x in found))
                else:
                    e, top = next(x for x in found if x[1].required_level <= ctx.ilvl and t.satisfied_by(x[0].mod))
                    good = {"elevated": e.mod, "influence": inf, "top": top, "label": label, "target": t}
            if why:
                unreach.append({"target": label, "reason": why})
                continue
            reach.append({"target": label, "affix": good["target"].affix, "sources": [orb]})
        if len(el_specs) > 1:
            conflicts.append(M("m.dom.two", orb=orb))
        if inf_specs or veil_specs or any(s.get("fractured") for s in exp_specs):
            conflicts.append(M("m.dom.with_other"))
        fams = ctx.families()
        for spec in exp_specs:
            f = fams.get(spec.get("signature"))
            if f is not None and f.influence:
                conflicts.append(M("m.dom.influenced_target", label=self._fam_label(f), orb=orb))
        if good is None or unreach or conflicts:
            return (reach, unreach, conflicts, slots), None
        side_n = sum(1 for spec in exp_specs if fams.get(spec.get("signature")) is not None
                     and fams[spec["signature"]].affix == good["top"].affix) + 1
        if side_n > ctx.limits[good["top"].affix]:
            side = good["top"].affix
            slots.append(M("m.dom.slots", n=side_n - 1, side=side, side_word=M(f"m.side.{side}"), max=ctx.limits[side]))
            return (reach, unreach, conflicts, slots), None
        return (reach, unreach, conflicts, slots), good

    def _dominance_strategies(self, ctx, targets, info, base_pool, exp_specs, dom: dict) -> list[Strategy]:
        """Every explicit strategy for: the explicit targets + the Elevated modifier's base at its highest tier +
        one sacrificial influenced modifier (the Orb needs two). Then the Orb of Dominance. Range plan."""
        top, inf = dom["top"], dom["influence"]
        n = len(targets)
        t_base = Target(n, top.signature, top.stats[0].id, top.stats[0].min, generic_text(top.text), top.affix)
        self._register_mods([top])
        self._tsuffix[(t_base.signature, t_base.index)] = M("m.dom.highest")
        used = set(top.groups)
        for x in targets:
            used |= self._provider_groups(ctx, x)
        sac = None
        for f in ctx.families().values():
            if not f.influence or "roll" not in f.sources or f.signature == top.signature:
                continue
            tx = Target(n + 1, f.signature, f.primary_stat, None, f.label, f.affix)
            if self._provider_groups(ctx, tx) & used:
                continue
            w = sum(e.weight for e in base_pool if tx.satisfied_by(e.mod))
            if w > 0 and (sac is None or w > sac[1]):
                sac = (tx, w)
        if sac is None:
            return []
        self._tsuffix[(sac[0].signature, sac[0].index)] = M("m.dom.sacrificed")
        extra = []
        for t in (t_base, sac[0]):
            extra.append({"roll_weight": sum(e.weight for e in base_pool if t.satisfied_by(e.mod)), "essences": [],
                          "bench": None})
        try:
            inner = self._explicit_strategies(ctx, list(targets) + [t_base, sac[0]], list(info) + extra, base_pool,
                                              exp_specs)
        except Infeasible:
            return []
        el = dom["elevated"]
        q_e = dom["target"].value_chance(el)
        price = self.prices.get(self.rules["dominance_orb"])
        params_d = {"elevated": el, "influence": inf, "top": top, "base_target": t_base, "sac_target": sac[0],
                    "label": dom["label"], "target": dom["target"], "price": price, "p_uniform": 1 / 2, "n": 2}
        notes = [self._range_note(self.rules["dominance_orb"]), self._note("dominance_data_note")]
        self._register_mods([el])
        mapping, out = {}, []
        for s in inner:
            q = s.params.get("value_q", 1.0) * q_e
            divines = (1 / q - 1) if q > 0 else float("inf")
            divines = divines if divines > 1e-6 else 0.0
            w = dataclasses.replace(
                s, family=s.family + "+dominance", cost_dominance=price, status=APPROX, sim=None,
                divines=divines, cost_divine=divines * self.prices.get("Divine Orb"),
                notes=list(s.notes) + notes,
                params=dict(s.params, inner=s, value_q=q, base_family=s.params.get("base_family", s.family),
                            dominance=params_d))
            mapping[id(s)] = w
            out.append(w)
        for w in out:
            ref = w.params.get("recovery_ref")
            if ref is not None:
                w.params["recovery_ref"] = mapping.get(id(ref), ref)
        return [w for w in out if w.p > 0 and w.cost < float("inf")]

    def _dominance_steps(self, s: Strategy, steps: list[dict], targets) -> list[dict]:
        d = s.params["dominance"]
        orb = CUR(self.rules["dominance_orb"])
        rg = self._range(s)
        el_target = self._dom_target_text(d)
        step = {
            "title": orb,
            "action": M("m.dom.step.action", base=self._td(d["base_target"]), sac=self._td(d["sac_target"]), orb=orb,
                        label=d["label"]),
            "detail": M("m.dom.step.detail", note=self._range_note(orb), text=rg["text"] if rg else "",
                        low=rg["low_reason"] if rg else "", mid=rg["assumed_reason"] if rg else "",
                        high=rg["high_reason"] if rg else "", data=self._note("dominance_data_note")),
            "probability_text": M("m.dom.step.chance", p=d["p_uniform"], n=d["n"]),
            "expected_state": M("m.dom.step.state", target=el_target),
            "on_success": M("m.step.next"),
            "on_fail": [{"text": M("m.dom.step.fail_redo", orb=orb), "status": APPROX},
                        {"text": M("m.dom.step.fail_more"), "status": NOT_COMPUTED}],
            "status": APPROX,
        }
        final = [t for t in targets if t.index not in (d["base_target"].index, d["sac_target"].index)]
        final_text = J([self._state(final) if final else "", d["label"]], ", ")
        pass_note = M("m.dom.pass_note", orb=orb)
        out = []
        for st in steps:          # step titles compare by their English text (Text == str)
            if step is not None and st.get("cost") and st["title"] != "Get the base":
                st = dict(st, detail=J([st.get("detail") or "", pass_note]))
            if st["title"] in ("Divine Orb", "Done") and step is not None:
                out.append(step)
                step = None
            if st["title"] in ("Divine Orb", "Done"):
                st = dict(st, expected_state=M("m.finished", state=final_text) if st["title"] == "Divine Orb"
                          else final_text)
            out.append(st)
        return out

    def _dom_target_text(self, d: dict):
        """The Elevated target's description as a mod label (localized by the Elevated mod's id)."""
        t, el = d["target"], d["elevated"]
        label = GL(el.id, t.label, el.text)
        return label if t.min_value is None else J([label, f" (>= {t.shown_min:g})"])

    def _conflict_part(self, data, ctx: ItemContext, spec: dict, fam, label: str):
        """Orb of Conflict route to a tier 2 / tier 1 Eldritch implicit, or (None, reason)."""
        side, mn = spec["eldritch"], spec.get("min")
        mn = None if mn in (None, "") else float(mn)
        tiers = {t: m for t, m in fam.tiers.items() if m.weight(ctx.tags) > 0}
        good = [t for t in (2, 1) if t in tiers and (mn is None or tiers[t].stats[0].max >= mn)]
        if not good:
            return None, None
        t3 = [o for o in eldritch.options(data, self.rules, ctx, dict(spec, min=None), self.prices)[0] or []
              if o.tier == 3]
        if not t3:
            return None, M("m.conf.no_t3", orb=CUR(self.rules["conflict_orb"]))
        other = {"exarch": "eater", "eater": "exarch"}[side]
        tier_of = self.rules["eldritch_tier_names"]
        lesser = [c for c in data.currencies if c.side == other and ctx.base.item_class in c.item_classes
                  and tier_of.get(c.tier_name) == 6]
        if not lesser:
            return None, M("m.conf.no_lesser", orb=CUR(self.rules["conflict_orb"]), side=N(SIDE_NAMES[other]),
                           cls=N(ctx.base.item_class))
        target_tier = max(good)
        return ConflictPart(label=label, side=side, currency=t3[0].currency, t3=t3[0], mod=tiers[target_tier],
                            other_currency=lesser[0], other_price=self.prices.get(lesser[0].name),
                            conflict_price=self.prices.get(self.rules["conflict_orb"]), target_tier=target_tier,
                            raises=3 - target_tier), None

    def _conflict_steps(self, o) -> list[dict]:
        side, other = N(SIDE_NAMES[o.side]), N(SIDE_NAMES[o.other_currency.side])
        orb = CUR(self.rules["conflict_orb"])
        t3cur, othercur = CUR(o.t3.currency.name), CUR(o.other_currency.name)
        t3mod = G("mod", o.t3.mod.id, o.t3.mod.text)
        return [{
            "title": M("m.conf.t3.title", side=side),
            "action": M("m.step.eld.action", cur=t3cur, side=side, mod=t3mod),
            "detail": M("m.conf.t3.detail", orb=orb, note=self._note("eldritch_note")),
            "probability": o.t3.p, "probability_text": M("m.step.per", p=_fmt_p(o.t3.p), cur=t3cur),
            "attempts": 1 / o.t3.p, "cost": o.t3.cost,
            "expected_state": M("m.step.eld.state", side=side, mod=t3mod),
            "on_success": M("m.step.next"),
            "on_fail": [{"text": M("m.conf.t3.fail", cur=t3cur), "status": COMPUTED}],
            "status": APPROX,
        }, {
            "title": M("m.step.eld.title", side=other),
            "action": M("m.conf.other.action", cur=othercur, other=other, orb=orb),
            "probability": 1.0, "probability_text": M("m.conf.other.chance"), "cost": o.other_price,
            "expected_state": M("m.conf.other.state", side=side, other=other),
            "on_fail": [], "status": COMPUTED,
        }, {
            "title": M("m.conf.orb.title_n", orb=orb, n=o.raises, other=other) if o.raises > 1 else orb,
            "action": M("m.conf.orb.action", orb=orb, side=side,
                        then=M("m.conf.orb.then", tier=o.target_tier) if o.raises > 1 else ""),
            "detail": M("m.conf.orb.detail", range_note=self._range_note(orb), n=o.raises, orb=orb, cur=othercur,
                        note=self._note("conflict_note")),
            "probability_text": M("m.conf.orb.chance"),
            "expected_state": M("m.conf.orb.state", side=side, mod=G("mod", o.mod.id, o.mod.text), tier=o.target_tier),
            "on_success": M("m.conf.orb.success"),
            "on_fail": [{"text": M("m.conf.orb.fail_raised", other=other, orb=orb), "status": NOT_COMPUTED},
                        {"text": M("m.conf.orb.fail_removed", other=other, cur=othercur), "status": COMPUTED}],
            "status": APPROX,
        }]

    # ------------------------------------------------------------ veiled modifiers
    def _veiled_report(self, ctx: ItemContext, veil_specs: list[dict], exp_specs: list[dict], inf_specs: list[dict]):
        """Feasibility of '(Veiled)' targets -> ((reachable, unreachable, conflicts, slots), Target | None)."""
        if not veil_specs:
            return ((), (), (), ()), None
        vdata = get_veiled()
        fams = veiled.all_families(vdata)
        tags = set(ctx.tags)
        reach, unreach, conflicts, slots = [], [], [], []
        good = None
        for spec in veil_specs:
            f = fams.get(spec.get("signature"))
            mn = spec.get("min")
            mn = None if mn in (None, "") else float(mn)
            if f:
                src = next((u.mod for u in f.mods if generic_text(u.mod.text) == f.label), f.mods[0].mod)
                name = GL(src.id, f.label, src.text)
                self._register_mods([u.mod for u in f.mods])
            else:
                name = spec.get("label") or str(spec.get("signature"))
            label = M("m.vl.label", name=name, thr=M("m.thr", v=_shown(spec)) if mn is not None else "")
            why = None
            if f is None:
                why = M("m.vl.not_unveiled")
            elif not f.generic:
                why = M("m.vl.member", members=", ".join(sorted(f.members)), aisling=CUR(self.rules["aisling"]),
                        vc=CUR(self.rules["veiled_chaos"]))
            else:
                t = Target(0, f.signature, f.primary_stat, mn, f.label, f.affix)
                generic = [u.mod for u in f.mods if u.member is None]
                here = [m for m in generic if m.spawn_weight(tags) > 0]
                if not here:
                    why = M("m.vl.not_pool", cls=N(ctx.base.item_class), base=G("base", ctx.base.id, ctx.base.name))
                elif not [m for m in here if m.required_level <= ctx.ilvl]:
                    why = M("m.vl.ilvl", lvl=min(m.required_level for m in here))
                elif not [m for m in here if m.required_level <= ctx.ilvl and t.satisfied_by(m)]:
                    top = max(t.stat(m).max for m in here if t.stat(m) is not None)
                    why = M("m.vl.below", top=top)
            if why:
                unreach.append({"target": label, "reason": why})
                continue
            reach.append({"target": label, "affix": f.affix,
                          "sources": [CUR(self.rules["aisling"]), CUR(self.rules["veiled_chaos"])]})
            good = (f, mn, label)
        if len(veil_specs) > 1:
            conflicts.append(M("m.vl.two"))
        if any(s.get("fractured") for s in exp_specs) or inf_specs:
            conflicts.append(M("m.vl.with_other"))
        if good is None or unreach or conflicts:
            return (reach, unreach, conflicts, slots), None
        f, mn, label = good
        fams_x = ctx.families()
        vt = Target(0, f.signature, f.primary_stat, mn, f.label, f.affix)
        hit_groups = [set(u.mod.groups) for u in f.mods if u.member is None and vt.satisfied_by(u.mod)]
        need = set.intersection(*hit_groups) if hit_groups else set()
        same_side = 0
        for i, spec in enumerate(exp_specs):
            fx = fams_x.get(spec.get("signature"))
            if fx is None:
                continue
            same_side += fx.affix == f.affix
            tx = Target(i, fx.signature, fx.primary_stat, None, fx.label, fx.affix)
            shared = need & self._provider_groups(ctx, tx)
            if shared:
                conflicts.append(M("m.vl.group", a=label, b=self._fam_label(fx), groups=", ".join(sorted(shared))))
        if same_side + 1 > ctx.limits[f.affix]:
            slots.append(M("m.vl.slots", n=same_side, side=f.affix, side_word=M(f"m.side.{f.affix}"),
                           max=ctx.limits[f.affix]))
        return (reach, unreach, conflicts, slots), (vt if not (conflicts or slots) else None)

    def _veiled_strategies(self, ctx, targets, info, base_pool, exp_specs, vt: Target) -> list[Strategy]:
        """(a) Aisling: every explicit strategy with a free slot kept on the veiled target's side, then Aisling's
        veiled modifier and the unveil; on a miss the cheaper of restarting and Annulment retries.
        (b) Veiled Chaos Orb spam until the other targets roll (a veiled modifier holds one slot of the target's
        side), then unveil; a miss rerolls. Unveil odds: chance the target is among the options."""
        P = self.prices.get
        vt = dataclasses.replace(vt, index=len(targets))
        blocked = set()
        for x in targets:
            blocked |= self._provider_groups(ctx, x)
        pool = veiled.unveil_pool(get_veiled(), ctx, vt.affix, blocked)
        k = self.rules["veiled_options"]
        p_u, q_u = veiled.chance_in_options(pool, vt, k)
        if p_u <= 0:
            return []
        rep = max(((m, w) for m, w in pool if vt.satisfied_by(m)), key=lambda x: x[1])[0]
        base = {"target": vt, "p_u": p_u, "q_u": q_u, "k": k, "pool": len(pool)}
        out: list[Strategy] = []

        # (a) Aisling into the target
        A, a = P(self.rules["aisling"]), P("Orb of Annulment")
        pseudo = BenchChoice(vt, None, rep, 0.0, 1.0, kind="veiled")
        try:
            inner = self._explicit_strategies(ctx, list(targets) + [vt],
                                              list(info) + [{"roll_weight": 0.0, "essences": [], "bench": pseudo}],
                                              base_pool, exp_specs)
        except Infeasible:
            inner = []
        inner = [s for s in inner if s.bench is pseudo]
        n = len(targets) + 1                      # mods on the item with a wrong unveil (fillers ignored)
        f_r, e_r = 1 / p_u, A / p_u
        f_a, e_a = 1 + (1 - p_u) * (n - 1) / (n * p_u), (A + (1 - p_u) * a) / p_u
        mapping, wrapped = {}, []
        for s in inner:
            restart, annul = s.cost_rng * f_r + e_r, s.cost_rng * f_a + e_a
            common = dict(base, kind="aisling", price=A, n=n, restart_cost=restart, annul_cost=annul)
            if annul < restart:
                v = dict(common, policy="annul", factor=f_a, extra=e_a, p_finish=p_u / (1 - (1 - p_u) / n),
                         apps=1 / p_u + (1 - p_u) / p_u)
            else:
                v = dict(common, policy="restart", factor=f_r, extra=e_r, p_finish=p_u, apps=1 / p_u)
            w = self._wrap_veiled(s, s.family + "+aisling", s.name, v,
                                  [self._note("aisling_note"), self._note("veiled_options_note"),
                                   self._note("veiled_restart_note"), _AISLING_REMOVE_NOTE])
            mapping[id(s)] = w
            wrapped.append(w)
        for w in wrapped:
            ref = w.params.get("recovery_ref")
            if ref is not None:
                w.params["recovery_ref"] = mapping.get(id(ref), ref)
        out += wrapped

        # (b) Veiled Chaos into the target
        vc, side = P(self.rules["veiled_chaos"]), self.rules["veiled_side_chance"]
        ph = (veiled.placeholder(vt.affix),)
        benches = [None] + [i["bench"] for i in info if i["bench"] and i["bench"].kind == "bench"]
        for bench in benches:
            rng = [t for t in targets if bench is None or t.index != bench.target.index]
            for s in self._chaos(ctx, targets, rng, bench, base_pool, fixed=ph):
                s = dataclasses.replace(s, family="veiled_chaos",
                                        name=M("m.strategy.chaos", chaos=CUR(self.rules["veiled_chaos"])),
                                        cost_rng=vc / s.p, params=dict(s.params, per=vc))
                v = dict(base, kind="veiled_chaos", policy="reroll", price=vc, side=side, factor=1 / (side * p_u),
                         extra=P("Orb of Alchemy"), p_finish=side * p_u, apps=1.0)
                out.append(self._wrap_veiled(s, "veiled_chaos", s.name, v,
                                             [self._note("veiled_chaos_note"), self._note("veiled_options_note")]))
        return [s for s in out if s.p > 0 and s.cost < float("inf")]

    def _wrap_veiled(self, s: Strategy, family: str, name: str, v: dict, notes: list[str]) -> Strategy:
        """`s` repeated v['factor'] times per finished item (restarts / rerolls after a missed unveil), plus
        v['extra'] (Aisling, Annulments, the first Alchemy). The unveiled value is Divined with the rest."""
        q_total = s.params.get("value_q", 1.0) * v["q_u"]
        divines = (1 / q_total - 1) if q_total > 0 else float("inf")
        divines = divines if divines > 1e-6 else 0.0
        return dataclasses.replace(
            s, family=family, name=name, p=s.p * v["p_finish"], cost_rng=s.cost_rng * v["factor"],
            cost_veiled=v["extra"], divines=divines, cost_divine=divines * self.prices.get("Divine Orb"),
            status=APPROX, sim=None, notes=list(s.notes) + notes,
            params=dict(s.params, inner=s, value_q=q_total, base_family=s.params.get("base_family", s.family),
                        veiled=v))

    @staticmethod
    def _veiled_repeat_note(s: Strategy):
        v = s.params.get("veiled")
        if not v:
            return ""
        if v["kind"] == "veiled_chaos":
            side = v["target"].affix
            return M("m.vl.repeat_chaos", side=side, side_word=M(f"m.side.{side}"), f=v["factor"])
        return M("m.vl.repeat_aisling_restart" if v["policy"] == "restart" else "m.vl.repeat_aisling_annul",
                 f=v["factor"], annul=CUR("Orb of Annulment"))

    def _unveil_step(self, s: Strategy) -> dict:
        v = s.params["veiled"]
        t = v["target"]
        vc = CUR(self.rules["veiled_chaos"])
        side_word = M(f"m.side.{t.affix}")
        if v["kind"] == "veiled_chaos":
            fail = [{"text": M("m.vl.fail_reroll", vc=vc), "status": COMPUTED},
                    {"text": M("m.vl.fail_recovery", vc=vc), "status": NOT_COMPUTED}]
            extra = M("m.vl.side_extra", side=t.affix, side_word=side_word, p=v["side"], vc=vc,
                      finish=_fmt_p(v["p_finish"]))
        else:
            fail, extra = self._aisling_branches(s), ""
        return {
            "title": M("m.vl.unveil.title"),
            "action": M("m.vl.unveil.action", side=t.affix, side_word=side_word, target=self._td(t), k=v["k"]),
            "detail": M("m.vl.unveil.detail", pool=v["pool"], side=t.affix, side_word=side_word, k=v["k"],
                        label=self._tl(t), p=_fmt_p(v["p_u"]), extra=extra, note=self._note("veiled_options_note")),
            "probability": v["p_u"], "probability_text": M("m.vl.per_unveil", p=_fmt_p(v["p_u"])),
            "attempts": 1 / v["p_u"],
            "expected_state": M("m.vl.unveil.state", target=self._td(t)),
            "on_success": M("m.step.next"), "on_fail": fail, "status": APPROX,
        }

    def _aisling_branches(self, s: Strategy) -> list[dict]:
        v = s.params["veiled"]
        tail = lambda chosen: M("m.vl.br.assumed" if chosen else "m.vl.br.dearer")
        restart = {"text": M("m.vl.br.restart", cost=v["restart_cost"], tail=tail(v["policy"] == "restart")),
                   "status": COMPUTED}
        annul = {"text": M("m.vl.br.annul", annul=CUR("Orb of Annulment"), n=v["n"], cost=v["annul_cost"],
                           tail=tail(v["policy"] == "annul")),
                 "status": APPROX}
        lock = {"text": M("m.vl.br.lock"), "status": NOT_COMPUTED}
        return ([restart, annul] if v["policy"] == "restart" else [annul, restart]) + [lock]

    def _aisling_steps(self, s: Strategy) -> list[dict]:
        v = s.params["veiled"]
        t = v["target"]
        side_word = M(f"m.side.{t.affix}")
        return [{
            "title": CUR(self.rules["aisling"]),
            "action": M("m.vl.ais.action", side=t.affix, side_word=side_word),
            "detail": M("m.vl.ais.detail", remove=_AISLING_REMOVE_NOTE, note=self._note("aisling_note"),
                        price=v["price"],
                        annul=M("m.vl.ais.annul", annul=CUR("Orb of Annulment")) if v["policy"] == "annul" else ""),
            "probability": 1.0, "probability_text": M("m.vl.ais.chance"),
            "attempts": 1 / v["p_u"], "cost": s.cost_veiled,
            "expected_state": M("m.vl.ais.state", side=t.affix, side_word=side_word),
            "on_success": M("m.step.next"), "on_fail": [], "status": APPROX,
        }, self._unveil_step(s)]


    # ------------------------------------------------------------ influence
    def _influence_targets(self, ctx: ItemContext, specs: list[dict], exp_specs: list[dict], eld_specs: list[dict]):
        """Resolve influence targets -> ([(spec, influence ItemContext, Target)], (reach, unreach, conflicts, slots))."""
        if not specs:
            return [], ((), (), (), ())
        items, reach, unreach, conflicts, slots = [], [], [], [], []
        seen: dict[str, str] = {}
        for spec in specs:
            inf = spec["influence"]
            if inf not in influence.INFLUENCES:
                raise ValueError(M("m.err.influence", inf=inf, choices=", ".join(influence.INFLUENCES)))
            name = N(inf.title())
            ctx_i = influence.influence_context(self.data, self.rules, ctx.base, ctx.ilvl, (inf,))
            t, why = influence.target_for(ctx_i, spec, 0)
            mn = spec.get("min")
            if t:
                self._register_mods([e.mod for e in influence.exalt_pool(ctx_i)])
                text = self._tl(t)
            else:
                text = spec.get("label") or self._sig_text(spec.get("signature"))
            label = M("m.inf.label", name=text, thr=M("m.thr", v=_shown(spec)) if mn not in (None, "") else "", inf=name)
            if t is None:
                unreach.append({"target": label, "reason": why or self._influence_missing(ctx, spec)})
                continue
            if eld_specs:
                unreach.append({"target": label, "reason": M("m.inf.with_eldritch")})
                continue
            if ctx.influences:
                unreach.append({"target": label, "reason": M(
                    "m.inf.preinfluenced", list=J([N(i.title()) for i in ctx.influences], " + "),
                    awakener=CUR(self.rules["awakener_orb"]))})
                continue
            if inf in seen:
                conflicts.append(M("m.inf.same_twice", a=seen[inf], b=label, inf=name))
                continue
            seen[inf] = label
            exalt = self.rules["influence_exalts"][inf]
            reach.append({"target": label, "affix": t.affix, "sources": [CUR(exalt)]})
            items.append((spec, ctx_i, t))
        if len(seen) > 2:
            conflicts.append(M("m.inf.too_many", n=len(seen), list=J([N(i.title()) for i in seen], ", "),
                               awakener=CUR(self.rules["awakener_orb"])))
        fams = ctx.families()
        for affix in ("prefix", "suffix"):
            n = sum(1 for s in exp_specs if s.get("signature") in fams and fams[s["signature"]].affix == affix)
            n += sum(1 for _, _, t in items if t.affix == affix)
            if n > ctx.limits[affix]:
                slots.append(M("m.slots.affix", n=n, side=affix, side_word=M(f"m.side.{affix}"), max=ctx.limits[affix]))
        return items, (reach, unreach, conflicts, slots)

    def _sig_text(self, sig) -> str:
        m = next((m for m in self.data.mods.values() if m.signature == sig), None)
        return generic_text(m.text) if m else str(sig)

    def _influence_missing(self, ctx: ItemContext, spec: dict) -> str:
        inf, sig = spec["influence"], spec.get("signature")
        name, cls = N(inf.title()), N(ctx.base.item_class)
        if sig in ctx.families():
            return M("m.inf.no_influence_needed", base=G("base", ctx.base.id, ctx.base.name), inf=inf.title())
        others = []
        for other in influence.INFLUENCES:
            if other == inf:
                continue
            c = influence.influence_context(self.data, self.rules, ctx.base, ctx.ilvl, (other,))
            if any(e.mod.signature == sig for e in influence.exalt_pool(c)):
                others.append(other.title())
        if others:
            return M("m.inf.other_influence", cls=cls, others=J([N(o) for o in others], " / "), inf=name)
        c100 = influence.influence_context(self.data, self.rules, ctx.base, 100, (inf,))
        lv = [e.mod.required_level for e in influence.exalt_pool(c100) if e.mod.signature == sig]
        if lv:
            return M("m.inf.ilvl", lvl=min(lv), ilvl=ctx.ilvl)
        same = [m for m in self.data.mods.values() if m.signature == sig]
        if same and all("Maven" in m.id for m in same):      # only Elevated versions carry this stat line
            return M("m.inf.elevated", note=self._note("dominance_note"))
        return M("m.inf.no_mod", cls=cls, inf=name)

    def _provider_groups(self, ctx: ItemContext, t: Target) -> set[str]:
        mods = [e.mod for e in ctx.base_entries] + [m for _, m in ctx.essence_options] + [m for _, m in ctx.bench_options]
        groups = {m.groups for m in mods if t.satisfied_by(m)}
        return set(next(iter(groups))) if len(groups) == 1 else set()   # only when every provider shares it

    def _conqueror_strategies(self, ctx, targets, info, base_pool, exp_specs, item) -> list[Strategy]:
        """Every explicit strategy, run with a free slot reserved for the influenced target, then the influence
        Exalt. A failed Exalt leaves the item influenced, so a failure restarts the whole plan on a fresh base."""
        spec, ctx_i, t0 = item
        inf = spec["influence"]
        t = dataclasses.replace(t0, index=len(targets))
        pool_i = influence.exalt_pool(ctx_i)
        hits = [e for e in pool_i if t.satisfied_by(e.mod)]
        rep = max(hits, key=lambda e: e.weight).mod
        exalt = self.rules["influence_exalts"][inf]
        pseudo = BenchChoice(t, None, rep, 0.0, 1.0, kind="influence", influence=inf)
        inner = self._explicit_strategies(ctx, list(targets) + [t],
                                          list(info) + [{"roll_weight": 0.0, "essences": [], "bench": pseudo}],
                                          base_pool, exp_specs)
        inner = [s for s in inner if s.bench is pseudo]
        blocked = set()
        for x in targets:
            blocked |= self._provider_groups(ctx, x)
        p_ex, q = influence.exalt_chance(t, pool_i, blocked)
        if p_ex <= 0:
            return []
        price = self.prices.get(exalt)
        mapping, out = {}, []
        for s in inner:
            w = self._wrap_influence(s, inf, exalt, p_ex, q, price, len(pool_i))
            mapping[id(s)] = w
            out.append(w)
        for w in out:
            ref = w.params.get("recovery_ref")
            if ref is not None:
                w.params["recovery_ref"] = mapping.get(id(ref), ref)
        return out

    def _wrap_influence(self, s: Strategy, inf, exalt, p_ex, q, price, pool_size) -> Strategy:
        q_total = s.params.get("value_q", 1.0) * q
        divines = (1 / q_total - 1) if q_total > 0 else float("inf")
        divines = divines if divines > 1e-6 else 0.0
        w = dataclasses.replace(
            s, family=s.family + "+" + inf, p=s.p * p_ex, cost_rng=s.cost_rng / p_ex, cost_influence=price / p_ex,
            divines=divines, cost_divine=divines * self.prices.get("Divine Orb"), status=APPROX, sim=None,
            notes=[n for n in s.notes] + [self._note("influence_exalt_note"), self._note("influence_restart_note")],
            params=dict(s.params, inner=s, value_q=q_total, base_family=s.params.get("base_family", s.family),
                        influence={"name": inf, "exalt": exalt, "p": p_ex, "q": q, "price": price,
                                   "pool": pool_size}))
        return w

    def _awakener_strategies(self, ctx, targets, base_pool, items) -> list[Strategy]:
        """Two influences: craft each influenced modifier on its own item with an influence Exalt (donor and
        target), then Awakener's Orb. The other explicit targets must come from the reforge; a miss restarts with a
        new donor and target."""
        P = self.prices.get
        donors, reps, qs = [], [], 1.0
        for spec, ctx_i, t in items:
            opts = self._conqueror_strategies(ctx, [], [], base_pool, [], (spec, ctx_i, t))
            if not opts:
                return []
            for o in opts:
                o.params["metrics"] = self._metrics(o)
            d = min(opts, key=lambda o: o.cost)
            donors.append((spec["influence"], t, d))
            reps.append(max((e for e in influence.exalt_pool(ctx_i) if t.satisfied_by(e.mod)),
                            key=lambda e: e.weight).mod)
            qs *= d.params["influence"]["q"]
        a, b = (spec["influence"] for spec, _, _ in items)
        ctx2 = influence.influence_context(self.data, self.rules, ctx.base, ctx.ilvl, (a, b))
        pool2 = ctx2.pool()
        reps = tuple(reps)
        if targets:
            gp = GroupedPool(pool2, list(targets), reps)
            p_r = gp.success(gp.roll(self.rare_n, ctx.limits), _mask(targets), None, ctx.limits)
        else:
            p_r = 1.0
        if p_r <= 0:
            return []
        awk = P(self.rules["awakener_orb"])
        donor_cost = sum(d.cost for _, _, d in donors)
        q_e, _ = self._divine(list(targets), pool2, reps)
        q_total = q_e * qs
        divines = (1 / q_total - 1) if q_total > 0 else float("inf")
        divines = divines if divines > 1e-6 else 0.0
        s = Strategy("awakener", M("m.strategy.awakener", orb=CUR(self.rules["awakener_orb"]), a=N(a.title()),
                                   b=N(b.title())), None, list(targets), p_r,
                     donor_cost / p_r, 0.0, divines, divines * P("Divine Orb"),
                     params={"entries": pool2, "value_q": q_total,
                             "awakener": {"donors": donors, "existing": reps, "per_attempt": donor_cost + awk,
                                          "price": awk, "influences": (a, b)}},
                     status=APPROX, notes=[self._note("awakener_note")], cost_influence=awk / p_r)
        return [s]

    def _influence_step(self, s: Strategy, targets_text: str) -> dict:
        i = s.params["influence"]
        name = N(i["name"].title())
        exalt = CUR(i["exalt"])
        return {
            "title": exalt,
            "action": M("m.inf.step.action", exalt=exalt, inf=name, target=self._td(s.bench.target)),
            "detail": M("m.inf.step.detail", pct=i["p"] * 100, pool=i["pool"], inf=name,
                        note=self._note("influence_exalt_note")),
            "probability": i["p"], "probability_text": M("m.step.per", p=_fmt_p(i["p"]), cur=exalt),
            "attempts": 1 / i["p"], "cost": s.cost_influence,
            "expected_state": M("m.state.rare_with", state=targets_text),
            "on_success": M("m.step.next"),
            "on_fail": [{"text": M("m.inf.step.fail_restart", inf=name), "status": COMPUTED},
                        {"text": M("m.inf.step.fail_annul", annul=CUR("Orb of Annulment"), exalt=CUR("Exalted Orb")),
                         "status": NOT_COMPUTED}],
            "status": APPROX,
        }

    def _awakener_steps(self, ctx, s: Strategy) -> list[dict]:
        a = s.params["awakener"]
        base = G("base", ctx.base.id, ctx.base.name)
        steps = [{"title": M("m.aw.bases.title"),
                  "action": M("m.aw.bases.action", base=base, ilvl=ctx.ilvl),
                  "expected_state": M("m.aw.bases.state", base=base), "status": COMPUTED, "on_fail": [],
                  "detail": M("m.aw.bases.detail")}]
        for n, (inf, t, d) in enumerate(a["donors"], 1):
            name = N(inf.title())
            steps.append({
                "title": M("m.aw.item.title", role=M("m.aw.donor" if n == 1 else "m.aw.second"), inf=name),
                "action": M("m.aw.item.action", base=base, inf=name, target=self._td(t), route=self._display_name(d)),
                "detail": M("m.aw.item.detail", note=self._note("influence_exalt_note")),
                "probability": d.p, "probability_text": M("m.aw.item.chance", p=_fmt_p(d.params["influence"]["p"])),
                "cost": d.cost,
                "expected_state": M("m.aw.item.state", base=base, inf=name, target=self._td(t)),
                "on_fail": [{"text": M("m.aw.item.fail"), "status": COMPUTED}],
                "status": APPROX,
            })
        rest = J([self._td(t) for t in s.rng_targets], ", ") if s.rng_targets else ""
        orb = CUR(self.rules["awakener_orb"])
        steps.append({
            "title": orb,
            "action": M("m.aw.orb.action", orb=orb),
            "detail": J([self._note("awakener_note"), M("m.aw.orb.detail_rest", rest=rest) if rest else None]),
            "probability": s.p, "probability_text": M("m.aw.orb.chance_rest", p=_fmt_p(s.p)) if rest else _fmt_p(s.p),
            "attempts": 1 / s.p, "cost": s.cost_rng + s.cost_influence,
            "expected_state": M("m.state.rare_with", state=J([self._td(t) for _, t, _ in a["donors"]]
                                                             + ([rest] if rest else []), ", ")),
            "on_success": M("m.step.next"),
            "on_fail": ([{"text": M("m.aw.orb.fail_restart"), "status": COMPUTED},
                         {"text": M("m.aw.orb.fail_alt", exalt=CUR("Exalted Orb"), annul=CUR("Orb of Annulment")),
                          "status": NOT_COMPUTED}] if rest else []),
            "status": APPROX,
        })
        return steps

    # ------------------------------------------------------------ feasibility
    def _feasibility(self, ctx: ItemContext, specs: list[dict]):
        fams = ctx.families()
        roll = {e.mod.id for e in ctx.base_entries}
        ess = {m.id for _, m in ctx.essence_options}
        bench = {m.id for _, m in ctx.bench_options}
        mods = [e.mod for e in ctx.base_entries] + [m for _, m in ctx.essence_options] + \
            [m for _, m in ctx.bench_options]
        reachable, unreachable, provs = [], [], []
        for i, spec in enumerate(specs):
            sig, mn = spec.get("signature"), spec.get("min")
            mn = None if mn in (None, "") else float(mn)
            fam = fams.get(sig)
            if fam is not None:
                name = self._fam_label(fam)
            else:   # not on this base: describe it from any mod with that stat line
                any_mod = next((m for m in self.data.mods.values() if m.signature == sig), None)
                name = spec.get("label") or (GL(any_mod.id, generic_text(any_mod.text), any_mod.text) if any_mod else sig)
            label = J([name, M("m.thr", v=mn) if mn is not None else None])
            if fam is None:
                unreachable.append({"target": label, "reason": self._why_missing(ctx, sig)})
                continue
            t = Target(i, sig, fam.primary_stat, mn, fam.label, fam.affix)
            p = [m for m in mods if t.satisfied_by(m)]
            if not p:
                top = fam.to_json()["max_value"]
                unreachable.append({"target": label, "reason": (
                    M("m.feas.below", top=top) if top is not None else M("m.feas.no_tier"))})
                continue
            sources = sorted({"roll" if m.id in roll else "essence" if m.id in ess else "bench" if m.id in bench
                              else "other" for m in p})
            reachable.append({"target": label, "affix": fam.affix, "sources": [M(f"m.src.{x}") for x in sources]})
            provs.append((label, sig, fam.affix, p))
        conflicts = []
        for (la, sa, _, pa), (lb, sb, _, pb) in itertools.combinations(provs, 2):
            if sa != sb and all(set(a.groups) & set(b.groups) for a in pa for b in pb):
                shared = sorted({g for a in pa for b in pb for g in set(a.groups) & set(b.groups)})
                conflicts.append(M("m.feas.group", a=la, b=lb, groups=", ".join(shared)))
        slots = []
        for affix in ("prefix", "suffix"):
            n = len({sig for _, sig, a, _ in provs if a == affix})
            if n > ctx.limits[affix]:
                slots.append(M("m.slots.affix", n=n, side=affix, side_word=M(f"m.side.{affix}"), max=ctx.limits[affix]))
        return reachable, unreachable, conflicts, slots

    def _report(self, ctx, specs, reachable, unreachable, conflicts, slots=(), extra=None) -> dict:
        if not (reachable or unreachable):
            reachable, unreachable, conflicts, slots = self._feasibility(ctx, specs)
        inf = M("m.inf.base_inf", list=J([N(i.title()) for i in ctx.influences], " + ")) if ctx.influences else ""
        return {"base": M("m.inf.base", base=G("base", ctx.base.id, ctx.base.name), ilvl=ctx.ilvl, inf=inf),
                "reachable": reachable,
                "unreachable": unreachable, "conflicts": list(conflicts), "slot_problems": list(slots),
                "extra": extra}

    def _diagnose(self, ctx: ItemContext, specs: list[dict], eldritch=((), (), ()),
                  influence=((), (), (), ()), fracture=((), (), (), ()), veil=((), (), (), ()),
                  dominance=((), (), (), ()), special_r=((), (), (), ())) -> dict | None:
        reachable, unreachable, conflicts, slots = self._feasibility(ctx, specs)
        reachable, unreachable, conflicts = (reachable + list(eldritch[0]), unreachable + list(eldritch[1]),
                                             conflicts + list(eldritch[2]))
        reachable, unreachable, conflicts = (reachable + list(influence[0]), unreachable + list(influence[1]),
                                             conflicts + list(influence[2]))
        unreachable, conflicts = unreachable + list(fracture[1]), conflicts + list(fracture[2])
        if fracture[1]:      # a target that cannot be the fractured one is not "reachable" as asked
            bad = {u["base_target"] for u in fracture[1]}
            reachable = [r for r in reachable if r["target"] not in bad]
        slots = list(slots) + [x for x in influence[3] if x not in slots]
        reachable, unreachable = reachable + list(veil[0]), unreachable + list(veil[1])
        conflicts, slots = conflicts + list(veil[2]), slots + list(veil[3])
        reachable, unreachable = reachable + list(dominance[0]), unreachable + list(dominance[1])
        conflicts, slots = conflicts + list(dominance[2]), slots + list(dominance[3])
        reachable, unreachable = reachable + list(special_r[0]), unreachable + list(special_r[1])
        conflicts, slots = conflicts + list(special_r[2]), slots + list(special_r[3])
        if unreachable or conflicts or slots:
            return self._report(ctx, specs, reachable, unreachable, conflicts, slots)
        return None

    def _why_missing(self, ctx: ItemContext, sig: str) -> str:
        if not any(m.signature == sig for m in self.data.mods.values()):
            return M("m.why.no_mod")
        same = [m for m in self.data.mods.values() if m.signature == sig]
        if all("Maven" in m.id for m in same):
            return M("m.inf.elevated", note=self._note("dominance_note"))
        rules = self.rules
        full = len(ctx.influences) >= rules["max_influences"]
        enabling = []
        for inf in rules["influences"]:
            if inf in ctx.influences:
                continue
            c2 = ItemContext(self.data, rules, ctx.base, ctx.ilvl, (inf,) if full else ctx.influences + (inf,))
            if sig in c2.families():
                enabling.append(inf.title())
        if enabling:
            hint = ("" if ctx.influences else
                    M("m.why.hint", inf=enabling[0], exalt=CUR(rules["influence_exalts"][enabling[0].lower()])))
            return M("m.why.needs_influence", list=J([N(e) for e in enabling], M("m.why.or")),
                     full=M("m.why.full", max=rules["max_influences"]) if full else "", hint=hint)
        c3 = ItemContext(self.data, rules, ctx.base, 100, ctx.influences)
        if sig in c3.families():
            lvl = min((e.mod.required_level for e in c3.base_entries if e.mod.signature == sig), default=100)
            return M("m.why.ilvl", lvl=lvl, ilvl=ctx.ilvl)
        return M("m.why.nothing", cls=N(ctx.base.item_class), base=G("base", ctx.base.id, ctx.base.name))

    # ------------------------------------------------------------- families
    def _chaos(self, ctx, targets, rng, bench, pool, fixed=()):
        """fixed: a fractured mod already on the (rare) item - kept by every reroll, no Alchemy needed."""
        p = self._reroll_p(ctx, targets, rng, bench, pool, existing=fixed)
        if p <= 0:
            return []
        per = self.prices.get("Chaos Orb")
        s = Strategy("chaos", M("m.strategy.chaos", chaos=CUR("Chaos Orb")), bench, rng, p,
                     (0.0 if fixed else self.prices.get("Orb of Alchemy")) + per / p, 0, 0, 0,
                     params={"entries": pool, "per": per, **({"fixed": fixed} if fixed else {})})
        return [self._finish(s, pool, fixed)]

    def _essences(self, ctx, targets, rng, bench, info, fixed=()):
        out = []
        pool = ctx.pool()
        seen = set()
        for t in rng:
            for ess, m in info[t.index]["essences"]:
                if ess.id in seen:
                    continue
                seen.add(ess.id)
                needs_scour = self._needs_normal(ess)
                if fixed and needs_scour:
                    continue      # lower essences need a Normal item; one with a fractured mod is not modelled
                p = self._reroll_p(ctx, targets, rng, bench, pool, existing=(m,) + tuple(fixed))
                if p <= 0:
                    continue
                per = self.prices.essence(ess.name, ess.level) + (self.prices.get("Orb of Scouring") if needs_scour else 0)
                s = Strategy("essence", M("m.strategy.essence", essence=G("base", ess.id, ess.name)), bench, rng, p,
                             per / p, 0, 0, 0,
                             params={"entries": pool, "essence": ess, "mod": m, "per": per, "scour": needs_scour,
                                     **({"fixed": fixed} if fixed else {})})
                out.append(self._finish(s, pool, existing=(m,) + tuple(fixed)))
        out.sort(key=lambda s: s.cost)
        return out[:2]

    def _fossil_cost(self, fossils: tuple[Fossil, ...]) -> float:
        res = self.rules["resonators"][str(len(fossils))]
        return sum(self.prices.get(f.name) for f in fossils) + self.prices.get(res)

    def _fossils(self, ctx, targets, rng, bench, fixed=()):
        skip = set(self.rules["fossils_not_modelled"])
        cands = [f for f in self.data.fossils if f.name not in skip and f.weights]
        scored = []
        for f in cands:
            pool = ctx.pool((f,))
            p = self._reroll_p(ctx, targets, rng, bench, pool, existing=fixed)
            if p > 0:
                scored.append((self._fossil_cost((f,)) / p, f))
        scored.sort(key=lambda x: x[0])
        top = [f for _, f in scored[: self.rules["fossil_search_candidates"]]]
        combos = [(f,) for f in top]
        for k in range(2, self.rules["fossil_search_max_combo"] + 1):
            combos += list(itertools.combinations(top, k))
        out = []
        for combo in combos:
            pool = ctx.pool(combo)
            p = self._reroll_p(ctx, targets, rng, bench, pool, existing=fixed)
            if p <= 0:
                continue
            per = self._fossil_cost(combo)
            s = Strategy("fossil", self._fossil_name(combo), bench, rng, p, per / p, 0, 0, 0,
                         params={"entries": pool, "fossils": combo, "per": per,
                                 "resonator": self.rules["resonators"][str(len(combo))],
                                 **({"fixed": fixed} if fixed else {})})
            s.notes.append(self._note("fossil_note"))
            out.append(self._finish(s, pool, fixed))
        out.sort(key=lambda s: s.cost)
        return out[:3]

    def _harvest(self, ctx, targets, rng, bench, pool, fixed=()):
        out = []
        tags = self.rules["harvest_reforge_tags"]
        per = self.prices.get(self.rules["harvest_price_key"])
        for tag, label in tags.items():
            if not any(tag in e.mod.tags for t in rng for e in pool if t.satisfied_by(e.mod)):
                continue
            p = self._reroll_p(ctx, targets, rng, bench, pool, existing=fixed, restrict=tag)
            if p <= 0:
                continue
            s = Strategy("harvest", M("m.strategy.harvest", tag=M(f"m.harvest.{tag}")), bench, rng, p,
                         (0.0 if fixed else self.prices.get("Orb of Alchemy")) + per / p, 0, 0, 0,
                         params={"entries": pool, "tag": tag, "label": label, "per": per,
                                 **({"fixed": fixed} if fixed else {})}, status=APPROX)
            s.notes.append(self._note("harvest_note"))
            out.append(self._finish(s, pool, fixed))
        out.sort(key=lambda s: s.cost)
        return out[:1]

    def _alt_regal(self, ctx, targets, rng, bench, pool):
        if not rng or any(not any(t.satisfied_by(e.mod) for e in pool) for t in rng):
            return []
        gp = GroupedPool(pool, targets, (), bench.groups if bench else ())
        bench_t = bench.target if bench else None
        pre = [t for t in rng if t.affix == "prefix"] + [None]
        suf = [t for t in rng if t.affix == "suffix"] + [None]
        magic_final = gp.roll(self.magic_n, ctx.magic_limits)
        price = self.prices.get
        out = []
        for a, b in itertools.product(pre, suf):
            alt_set = [t for t in (a, b) if t is not None]
            if not alt_set:
                continue
            amask = _mask(alt_set)
            rest = [t for t in rng if t not in alt_set]
            succ: dict = defaultdict(float)
            p_aug = 0.0
            for s, p in magic_final.items():
                if s[0] & gp.block_mask:
                    continue
                if s[1] & amask == amask:
                    succ[s] += p
                    continue
                missing = [t for t in alt_set if not s[1] >> t.index & 1]
                np_, ns_ = gp.counts(s)
                if len(missing) == 1 and np_ + ns_ == 1 and (np_ if missing[0].affix == "prefix" else ns_) == 0:
                    if s[1] & amask == 0:
                        continue   # the single mod is junk: re-alt instead of augmenting
                    p_aug += p
                    total, opts = gp.options(s, ctx.magic_limits, only_affix=missing[0].affix)
                    for w, ns in opts:
                        if ns[1] & amask == amask and not ns[0] & gp.block_mask:
                            succ[ns] += p * w / total
            p_a = sum(succ.values())
            if p_a <= 0:
                continue
            start = {s: p / p_a for s, p in succ.items()}
            steps = len(rest) or (1 if bench else 0)
            final, reach = gp.slam_chain(start, ctx.limits, steps, _mask(rest))
            p_c = gp.success(final, _mask(rng), bench_t, ctx.limits) if steps else \
                gp.success(start, _mask(rng), bench_t, ctx.magic_limits)
            if p_c <= 0:
                continue
            e_magic = (price("Orb of Alteration") + (p_aug / 1.0) * price("Orb of Augmentation")) / p_a
            slam_spend = sum(r * (price("Regal Orb") if j == 0 else price("Exalted Orb")) for j, r in enumerate(reach))
            per_full = price("Orb of Transmutation") + e_magic + slam_spend + (1 - p_c) * price("Orb of Scouring")
            name = M("m.strategy.alt", alt_cur=CUR("Orb of Alteration"),
                     regal=M("m.strategy.alt_regal", regal_cur=CUR("Regal Orb")) if steps else "",
                     exalt=M("m.strategy.alt_exalt", exalt_cur=CUR("Exalted Orb")) if len(rest) > 1 else "")
            s = Strategy("alt", name, bench, rng, p_a * p_c, per_full / p_c, 0, 0, 0, params={
                "entries": pool, "alt_set": alt_set, "rest": rest, "p_alt": p_a, "p_aug": p_aug,
                "p_chain": p_c, "reach": reach, "steps": steps, "e_magic": e_magic, "start": start,
            })
            out.append(self._finish(s, pool))
        out.sort(key=lambda s: s.cost)
        return out[:1]

    def _multimod(self, ctx, targets, rng, benches, info, pool, fossil_combos):
        """Reroll families finished with two bench crafts ('Can have up to 3 Crafted Modifiers')."""
        mm = self._craft_cost(self.rules["metacrafts"]["multimod"])
        if mm is None:
            return []
        P = self.prices.get
        variants = [("chaos", pool, (), P("Chaos Orb"), P("Orb of Alchemy"), M("m.strategy.chaos", chaos=CUR("Chaos Orb")),
                     {})]
        seen = set()
        for t in rng:
            for ess, m in info[t.index]["essences"]:
                if ess.id in seen:
                    continue
                seen.add(ess.id)
                scour = self._needs_normal(ess)
                per = self.prices.essence(ess.name, ess.level) + (P("Orb of Scouring") if scour else 0)
                variants.append(("essence", pool, (m,), per, 0.0, M("m.strategy.essence", essence=G("base", ess.id, ess.name)),
                                 {"essence": ess, "mod": m, "scour": scour}))
        for combo in fossil_combos:
            variants.append(("fossil", ctx.pool(combo), (), self._fossil_cost(combo), 0.0, self._fossil_name(combo),
                             {"fossils": combo, "resonator": self.rules["resonators"][str(len(combo))]}))
        out = []
        groups = tuple(g for b in benches for g in b.groups)
        for fam, entries, existing, per, prep, name, extra in variants:
            gp = GroupedPool(entries, targets, existing, groups)
            final = gp.roll(self.rare_n, ctx.limits)
            bl = [(b.target, gp.mask_for_groups(b.groups)) for b in benches]
            p = fin = p_mm = apps = 0.0
            for st, pr in final.items():
                ok, pending = gp.multi_success(st, _mask(rng), bl, ctx.limits)
                if ok:
                    idx = {t.index for t in pending}
                    p += pr
                    p_mm += pr if len(idx) >= 2 else 0.0
                    fin += pr * (sum(b.cost for b in benches if b.target.index in idx) + (mm if len(idx) >= 2 else 0))
                    apps += pr * (len(idx) + (1 if len(idx) >= 2 else 0))
            if p <= 0:
                continue
            # when the slots never allow both crafts, Multimod is never bought: say what really happens
            label = M("m.strategy.multimod" if p_mm > 1e-9 else "m.strategy.bench_missing", name=name)
            s = Strategy(fam, label, benches[0], rng, p, prep + per / p, 0, 0, 0,
                         params={"entries": entries, "per": per, "bench2": benches[1], "multimod_cost": mm,
                                 "p_multimod": p_mm / p, "apps_bench": apps / p, **extra})
            self._finish(s, entries, existing)
            s.cost_bench = fin / p
            out.append(s)
        return out

    # ------------------------------------------------------ recovery variants
    def _recovery_variant(self, ctx, targets, s: Strategy) -> Strategy | None:
        fam = s.family
        if fam not in ("chaos", "essence", "fossil", "harvest", "alt") or "bench2" in s.params:
            return None
        P = self.prices.get
        base_pool = ctx.pool()
        bench = s.bench
        bt = bench.target if bench else None
        fixed = tuple(s.params.get("fixed", ()))          # a fractured mod: kept, never Annulled or Scoured
        existing = ((s.params["mod"],) if fam == "essence" else ()) + fixed
        gp = GroupedPool(base_pool, targets, existing, bench.groups if bench else (), protected=fixed)
        L = ctx.limits
        req = _mask(s.rng_targets)
        if fam == "alt":
            if not s.params["steps"]:
                return None
            d0: dict = defaultdict(float)
            p_regal_hit = 0.0
            need = _mask(s.params["rest"])
            for st, p in s.params["start"].items():
                total, opts = gp.options(st, L)
                for w, ns in opts:
                    d0[ns] += p * w / total
                    if ns[1] & need & ~st[1]:
                        p_regal_hit += p * w / total
            c_attempt = P("Orb of Transmutation") + s.params["e_magic"] + P("Regal Orb")
            restart, prefix = P("Orb of Scouring"), 0.0
        else:
            roll_gp = gp if fam != "fossil" else GroupedPool(s.params["entries"], targets, existing,
                                                             bench.groups if bench else (), layout=gp,
                                                             protected=fixed)
            d0 = roll_gp.roll(self.rare_n, L, s.params.get("tag"))
            c_attempt, restart = s.params["per"], 0.0
            prefix = P("Orb of Alchemy") if fam in ("chaos", "harvest") and not fixed else 0.0
            p_regal_hit = None
        v_upper = s.cost_rng - prefix
        mdp = RecoveryMDP(gp, L, self.rare_n, self._mdp_costs(ctx.base.item_class), restart,
                          lambda st: gp.is_success(st, req, bt, L))
        left = min(self.rec_time_limit, self.rec_phase_limit - self._rec_spent)
        t_solve = time.monotonic()
        mdp.deadline = t_solve + max(0.0, left)
        try:
            v = mdp.solve(d0, c_attempt, v_upper)
        except MdpTooLarge as e:
            if e.timed_out:
                s.notes.append(M("m.rec.time_limit", secs=self.rec_time_limit, total=self.rec_phase_limit))
                if s.name not in self._rec_skipped:
                    self._rec_skipped.append(s.name)
            else:
                s.notes.append(M("m.rec.too_large", error=str(e)))
            return None
        finally:
            self._rec_spent += time.monotonic() - t_solve
            mdp.deadline = None
        if not v < v_upper * (1 - 1e-4):
            s.notes.append(M("m.rec.never_cheaper"))
            return None
        usage = mdp.usage()
        used = [d["action"] for d in usage["actions"].values() if d["uses"] > 1e-6]
        annuls_essence = fam == "essence" and any("annul" in a.key for a in used)
        status = APPROX if s.status == APPROX or annuls_essence or any(a.status == APPROX for a in used) else COMPUTED
        r = Strategy(fam + "+rec", s.name, bench, s.rng_targets, mdp.p_attempt, prefix + v, 0, 0, 0,
                     params={**s.params, "base_family": fam, "mdp": mdp, "gp": gp, "usage": usage,
                             "c_attempt": c_attempt, "restart_cost": restart, "base_cost": s.cost,
                             "p_regal_hit": p_regal_hit},
                     status=status, notes=[n for n in s.notes if "recovery" not in n])
        r.notes.append(M("m.rec.policy_note"))
        if any(a.status == APPROX for a in used):
            r.notes.append(_LOCK_CHAOS_NOTE)
        if annuls_essence:
            r.notes.append(_ESSENCE_ANNUL_NOTE)
        return self._finish(r, s.params["entries"], existing)

    # ============================================================ simulation
    # ------------------------------------------------------------ simulation time limits
    def _sim_budget_left(self, s: Strategy) -> bool:
        """False once the simulation phase has used its time: `s` is then left unsimulated (and noted)."""
        if self._sim_spent < self.sim_phase_limit:
            return True
        if s.name not in self._sim_skipped:
            if not self._sim_skipped:
                self.progress.timed_out()          # the UI / CLI show "Simulation timed out" from here on
            self._sim_skipped.append(s.name)
            s.notes.append(M("m.sim.phase_skipped", secs=self.sim_phase_limit))
        return False

    def _late(self) -> bool:
        return time.monotonic() > self._deadline

    def _count(self, n: int, trial) -> tuple[int, int, bool]:
        """Run trial() up to n times -> (hits, runs done, timed out). The clock is read every 64 runs."""
        hits = 0
        for i in range(n):
            if i and not i & 63 and self._late():
                return hits, i, True
            hits += trial()
        return hits, n, False

    def _mark_timeout(self, res: dict, target: int) -> None:
        res["timed_out"] = True
        res["target"] = target
        res["low_confidence"] = M("m.sim.timeout", secs=self.sim_time_limit, n=res["n"], target=target)

    def _timeout_warnings(self) -> list:
        out = []
        if self._rec_skipped:
            out.append(M("m.warn.rec_time", secs=self.rec_time_limit, names=J(self._rec_skipped, "; ")))
        if self._timeouts:
            out.append(M("m.warn.sim_timeout", secs=self.sim_time_limit, names=J(self._timeouts, "; ")))
        if self._sim_skipped:
            out.append(M("m.warn.sim_skipped", secs=self.sim_phase_limit, names=J(self._sim_skipped, "; ")))
        return out

    def _simulate(self, ctx: ItemContext, targets: list[Target], s: Strategy):
        if self.sims <= 0:
            return
        if self.progress.phase != "simulating":
            self.progress.start("simulating", self._sim_estimate)
            # the phase stops starting new simulations after sim_phase_limit; the last one may run sim_time_limit
            self.progress.ends_by = time.monotonic() + self.sim_phase_limit + self.sim_time_limit
        t = time.monotonic()
        self._deadline = t + self.sim_time_limit
        try:
            self._simulate_strategy(ctx, targets, s)
        finally:
            self._sim_spent += time.monotonic() - t
            self._deadline = float("inf")
        parts = [s.sim] if s.sim and "n" in s.sim else [v for v in (s.sim or {}).values() if isinstance(v, dict)]
        if any(x.get("timed_out") for x in parts):
            self._timeouts.append(s.name)
            self.progress.timed_out()
        self.progress.step()

    def _simulate_strategy(self, ctx: ItemContext, targets: list[Target], s: Strategy):
        n = self.sims
        if "mdp" in s.params:
            return self._simulate_mdp(ctx, s)
        sim = Simulator(s.params["entries"], seed=self.seed)
        bench_t = s.bench.target if s.bench else None
        bgroups = s.bench.groups if s.bench else ()
        lim = ctx.limits
        fam = s.params.get("base_family", s.family)
        if fam == "awakener":     # the reforge after Awakener's Orb, keeping one influenced mod from each item
            if not s.rng_targets:
                return
            ex = s.params["awakener"]["existing"]
            hits, done, late = self._count(n, lambda: satisfies(sim.roll(self.rare_n, lim, ex), s.rng_targets, None,
                                                                lim))
            s.sim = self._sim_json(s.p, hits, done)
            if late:
                self._mark_timeout(s.sim, n)
            return
        if fam != "alt":
            existing = ((s.params["mod"],) if "mod" in s.params else ()) + tuple(s.params.get("fixed", ()))
            restrict = s.params.get("tag")
            if "bench2" in s.params:
                benches = [(b.target, b.groups) for b in (s.bench, s.params["bench2"])]
                hits, done, late = self._count(n, lambda: satisfies_multi(
                    sim.roll(self.rare_n, lim, existing, restrict), s.rng_targets, benches, lim))
            else:
                hits, done, late = self._count(n, lambda: satisfies(
                    sim.roll(self.rare_n, lim, existing, restrict), s.rng_targets, bench_t, lim, bgroups))
            # an influence-Exalt variant simulates its explicit part, so compare with that part's own odds
            s.sim = self._sim_json(s.params["inner"].p if "inner" in s.params else s.p, hits, done)
            if late:
                self._mark_timeout(s.sim, n)
            return
        alt_set, rest, steps = s.params["alt_set"], s.params["rest"], s.params["steps"]
        mlim = ctx.magic_limits
        successes, attempts = [], 0
        late_alt = late_chain = False          # each part is marked only if the time limit cut that part
        while attempts < n * 3 and len(successes) < max(200, n // 10):
            if attempts and not attempts & 63 and self._late():
                late_alt = True
                break
            attempts += 1
            mods = self._alt_once(sim, alt_set, mlim, bgroups)[0]
            if mods is not None:
                successes.append(mods)
        chain_hits = chained = 0
        for mods in successes:
            if chained and not chained & 63 and self._late():
                late_chain = True
                break
            chained += 1
            mods = list(mods)
            ok = True
            for _ in range(steps):
                need = [t for t in rest if not any(t.satisfied_by(m) for m in mods)]
                m = sim.pick(mods, lim)
                if m is None:
                    ok = False
                    break
                mods.append(m)
                if need:
                    ok = any(t.satisfied_by(m) for t in need)
                else:
                    ok = not any(g in bgroups for g in m.groups)
                if not ok:
                    break
            if ok and satisfies(mods, s.rng_targets, bench_t, lim if steps else mlim, bgroups):
                chain_hits += 1
        s.sim = {
            "alt": self._sim_json(s.params["p_alt"], len(successes), attempts),
            "chain": self._sim_json(s.params["p_chain"], chain_hits, chained),
        }
        # targets of the two parts: up to 3n Alterations to collect hits, then one chain per collected hit
        if late_alt:
            self._mark_timeout(s.sim["alt"], n * 3)
        if late_chain:
            self._mark_timeout(s.sim["chain"], len(successes))
        s.sim["disagrees"] = s.sim["alt"]["disagrees"] or s.sim["chain"]["disagrees"]

    def _alt_once(self, sim: Simulator, alt_set, mlim, bgroups):
        """One Alteration (+ Augment when the analytic plan would). Returns (mods or None, used_aug)."""
        mods = sim.roll(self.magic_n, mlim)
        if any(g in bgroups for m in mods for g in m.groups):
            return None, False
        missing = [t for t in alt_set if not any(t.satisfied_by(m) for m in mods)]
        aug = False
        if len(missing) == 1 and len(mods) == 1 and len(missing) < len(alt_set) and mods[0].affix != missing[0].affix:
            aug = True
            m = sim.pick(mods, mlim, only_affix=missing[0].affix)
            if m is not None and not any(g in bgroups for g in m.groups):
                mods.append(m)
            missing = [t for t in alt_set if not any(t.satisfied_by(m) for m in mods)]
        return (None if missing else mods), aug

    def _simulate_mdp(self, ctx: ItemContext, s: Strategy):
        """Replay the computed recovery policy on concrete items; compare items per attempt and cost."""
        n = self.sims
        mdp, gp, fam = s.params["mdp"], s.params["gp"], s.params["base_family"]
        L, mlim = ctx.limits, ctx.magic_limits
        P = self.prices.get
        sim_roll = Simulator(s.params["entries"], seed=self.seed)    # reroll weights (fossils apply here only)
        sim = Simulator(ctx.pool(), seed=self.seed + 1)              # Exalt / Chaos after a metacraft: normal weights
        fixed = ((s.params["mod"],) if fam == "essence" else ()) + tuple(s.params.get("fixed", ()))
        bgroups = s.bench.groups if s.bench else ()
        attempts = items = fallbacks = steps = 0
        spent = last = 0.0
        item_costs = []        # cost of each finished item (spend since the previous one) - bookkeeping only
        budget = n * 40
        # Safety cap only: ~20 Alterations per attempt at a 5% hit rate fits easily; rare Alteration
        # targets (e.g. 0.25%) stop early and are flagged low-confidence below instead of running for minutes.
        alts_left = n * 100
        late = False
        while attempts < n and steps < budget:
            if attempts and not attempts & 63 and self._late():
                late = True
                break
            if fam == "alt":
                mods, cost = None, P("Orb of Transmutation")
                while alts_left > 0:
                    alts_left -= 1
                    got, aug = self._alt_once(sim, s.params["alt_set"], mlim, bgroups)
                    cost += P("Orb of Alteration") + (P("Orb of Augmentation") if aug else 0)
                    if got is not None:
                        mods = got
                        break
                if mods is None:
                    break          # budget used up mid-attempt: that attempt is not counted
                attempts += 1
                spent += cost
                spent += P("Regal Orb")
                m = sim.pick(mods, L)
                if m is not None:
                    mods = mods + [m]
            else:
                attempts += 1
                spent += s.params["c_attempt"]
                mods = [m for m in sim_roll.roll(self.rare_n, L, fixed, s.params.get("tag")) if m not in fixed]
            while True:
                steps += 1
                st = gp.state_of(mods)
                if mdp.done(st):
                    items += 1
                    item_costs.append(spent - last)
                    last = spent
                    break
                if st not in mdp.idx:
                    fallbacks += 1
                a = mdp.action_at(st)
                if a is None or steps > budget:
                    spent += s.params["restart_cost"]
                    break
                spent += a.cost
                mods = self._apply(sim, a, mods, fixed, L, tuple(s.params.get("fixed", ())))
                if mods is None:
                    spent += s.params["restart_cost"]
                    break
        res = self._sim_json(mdp.p_attempt, items, attempts)
        sim_cost = spent / items if items else None
        analytic_cost = mdp.vstart
        res.update({"cost_simulated": sim_cost, "cost_analytic": analytic_cost, "fallbacks": fallbacks})
        if len(item_costs) >= 2:
            k = len(item_costs)
            mean = sum(item_costs) / k
            sd = math.sqrt(sum((c - mean) ** 2 for c in item_costs) / (k - 1))
            res["cost_per_item"] = {"mean": mean, "lo": mean - 1.96 * sd / math.sqrt(k),
                                    "hi": mean + 1.96 * sd / math.sqrt(k), "sd": sd,
                                    "p95": sorted(item_costs)[min(k - 1, int(0.95 * k))]}
        if items >= 20 and abs(sim_cost - analytic_cost) > 0.25 * analytic_cost:
            res["disagrees"] = True
        self._flag_low_confidence(res, n)
        if late:
            self._mark_timeout(res, n)
        s.sim = res

    @staticmethod
    def _flag_low_confidence(res: dict, target: int) -> None:
        """A simulation budget stopped this strategy short of the attempt target: say so and how wide the CI is."""
        if res["n"] < target:
            lo, hi = res["ci"]
            res["low_confidence"] = M("m.sim.low_conf", n=res["n"], target=target, width=(hi - lo) * 100)

    def _apply(self, sim: Simulator, a, mods: list[Mod], fixed: tuple[Mod, ...], L: dict,
               protected: tuple[Mod, ...] = ()) -> list[Mod] | None:
        """Concrete version of an MdpAction. None = something unrepresentable was destroyed (restart)."""
        k = a.key
        everything = list(mods) + list(fixed)
        if k == "slam" or k.startswith("slam_no_"):
            lim, restrict = L, None
            if k.startswith("slam_no_"):
                lim = {"prefix": L["prefix"], "suffix": L["suffix"] - 1}
                restrict = "!" + k[len("slam_no_"):]
            m = sim.pick(everything, lim, restrict)
            return mods + [m] if m is not None else mods
        removable = tuple(m for m in fixed if m not in protected)    # fractured mods are never removed
        if k == "annul" or k.startswith("lock_annul_"):
            locked = None if k == "annul" else k.rsplit("_", 1)[1]
            out, hit = sim.annul(mods, removable, locked=locked, extra=0 if locked is None else 1)
            return None if hit == "fixed" else out
        keep = k.rsplit("_", 1)[1]
        if any(m.affix != keep for m in removable):
            return None
        kept = scour_side(mods, keep)
        if k.startswith("lock_scour_"):
            return kept
        new = sim.roll(self.rare_n, L, tuple(kept) + fixed)   # lock_chaos
        return [m for m in new if m not in fixed]

    @staticmethod
    def _sim_json(p_analytic: float, hits: int, n: int) -> dict:
        lo, hi = wilson(hits, n)
        # only flag real disagreements: outside the 95% CI and more than 15% relative
        disagree = n > 0 and hits >= 20 and not (lo <= p_analytic <= hi) and \
            abs(p_analytic - hits / n) > 0.15 * max(p_analytic, hits / n)
        return {"n": n, "hits": hits, "p": hits / n if n else 0.0, "ci": [lo, hi],
                "analytic": p_analytic, "disagrees": disagree, "reliable": hits >= 20}

    # ============================================================ steps / json
    def _state(self, targets, extra="") -> Text:
        s = J([self._td(t) for t in targets], ", ") if targets else M("m.state.none")
        return J([s, M("m.state.extra", extra=extra) if extra else None])

    @staticmethod
    def _display_name(s: Strategy) -> Text:
        name = [s.name]
        if s.params.get("veiled", {}).get("kind") == "veiled_chaos":
            name.append(M("m.name.unveil"))
        if "bench2" in s.params:
            name.append(M("m.name.two_bench") if s.params["p_multimod"] > 1e-9 else None)
        elif s.bench and s.bench.kind == "bench":
            name.append(M("m.name.bench"))
        if "mdp" in s.params:
            name.append(M("m.name.recovery"))
        if s.bench and s.bench.kind == "influence":
            name.append(M("m.name.plus", what=CUR(s.params["influence"]["exalt"]) if "influence" in s.params
                          else M("m.name.influence_exalt")))
        if s.bench and s.bench.kind == "veiled":
            name.append(M("m.name.aisling"))
        if "eldritch" in s.params and s.family != "eldritch":
            name.append(M("m.name.eldritch", list=J([CUR(o.currency.name) for o in s.params["eldritch"]], ", ")))
        if any(getattr(o, "conflict", False) for o in s.params.get("eldritch", ())):
            name.append(M("m.name.plus", what=CUR("Orb of Conflict")))
        if "dominance" in s.params:
            name.append(M("m.name.plus", what=CUR("Orb of Dominance")))
        return J(name)

    def _strategy_json(self, ctx, targets, s: Strategy, base_pool) -> dict:
        steps = self._steps(ctx, targets, s, base_pool)
        if "dominance" in s.params:
            steps = self._dominance_steps(s, steps, targets + [s.params["dominance"]["base_target"],
                                                               s.params["dominance"]["sac_target"]])
        self._number_steps(steps)
        m = s.params.get("metrics", {})
        out = {
            "family": s.family, "name": self._display_name(s),
            # this plan's own numbers leave the optional Divines out; ranking (comparison table) includes them
            "metrics": {"cost": m.get("cost_base", m.get("cost")), "cost_sd": m.get("sd"),
                        "bad_luck_cost": m.get("risk_base", m.get("risk")),
                        "applications": m.get("apps_base", m.get("apps")),
                        "with_optional_divine": {"cost": m.get("cost"), "bad_luck_cost": m.get("risk"),
                                                 "applications": m.get("apps")}},
            "simulated_cost": self._sim_cost(s),
            "status": s.status, "expected_cost": s.cost,
            "starts_from_normal": self._starts_from_normal(s),
            "optional_divine": ({"cost": s.cost_divine, "divines": s.divines, "p_values_met": s.params.get("value_q"),
                                 "total_with": s.cost_with_divine} if s.divines > 0 else None),
            "cost_breakdown": {"rng": s.cost_rng, "bench": s.cost_bench, "divine": s.cost_divine,
                               "eldritch": s.cost_eldritch, "influence": s.cost_influence,
                               "fracture": s.cost_fracture, "veiled": s.cost_veiled, "dominance": s.cost_dominance},
            "p_attempt": s.p, "divines": s.divines, "notes": s.notes, "sim": s.sim, "steps": steps,
        }
        rg = self._range(s)
        out["cost_range"] = rg
        if rg:          # never a single-number cost for a range plan
            out["expected_cost"] = None
            out["simulated_cost"] = None
            out["metrics"].update(cost=None, cost_sd=None, bad_luck_cost=None)
        if "mdp" in s.params:
            u = s.params["usage"]
            out["recovery"] = {
                "first_action_share": u["first"],
                "actions": [{"key": k, "label": d["action"].label, "status": d["action"].status,
                             "uses_per_item": d["uses"], "p_progress": d["p_progress"], "spend": d["spend"]}
                            for k, d in u["actions"].items()],
            }
        return out

    def _starts_from_normal(self, s: Strategy) -> bool:
        """Alteration plans start from a Normal item (Transmutation), and so do Whispering-Wailing essences."""
        fam = s.params.get("base_family", s.family)
        return fam.startswith("alt") or bool(s.params.get("scour"))

    @staticmethod
    def _guaranteed_target(s: Strategy):
        """The target an essence strategy's guaranteed mod satisfies (always on the item after the essence), or None."""
        m = s.params.get("mod") if "essence" in s.params else None
        return next((t for t in s.rng_targets if m is not None and t.satisfied_by(m)), None)

    @staticmethod
    def _spec_stat(ctx: ItemContext, spec: dict, fams: dict) -> str | None:
        """The primary stat a target spec's threshold applies to (what its Target will compare)."""
        sig = spec.get("signature") or ""
        if spec.get("eldritch"):
            return sig.split("|", 1)[1] if "|" in sig else None          # Eldritch keys: "<side>|<primary stat>"
        fam = fams.get(sig)
        if fam is not None:
            return fam.primary_stat
        stats = sig.split("|", 1)[1].split(",") if "|" in sig else []
        return stats[0] if len(stats) == 1 else None                    # influence / veiled / ... single-stat lines

    def _specs_to_stored(self, ctx: ItemContext, specs: list[dict]) -> list[dict]:
        """Thresholds arrive as the game displays them ('1.2' metres, '0.2' %); the planner compares stored values."""
        fams = ctx.families()
        out = []
        for spec in specs:
            mn = spec.get("min")
            if mn not in (None, ""):
                # min: stored units (compared); min_shown: as typed / displayed (used in labels)
                spec = dict(spec, min=to_stored(self._spec_stat(ctx, spec, fams), float(mn)), min_shown=float(mn))
            out.append(spec)
        return out

    @staticmethod
    def _number_steps(steps: list[dict]) -> None:
        """Main steps are numbered 1, 2, 3 ...; a recovery branch gets no number of its own, only its parent's."""
        n = 0
        for st in steps:
            if st.get("kind") == "recovery":
                continue
            st["kind"] = "main"
            n += 1
            st["number"] = n
        for st in steps:
            if st["kind"] == "recovery":
                st["number"] = None
                st["branch_of"] = steps[st["branch_of"]]["number"]

    @staticmethod
    def _action_labels(mdp: RecoveryMDP) -> dict:
        return {a.key: a for acts in mdp.acts if acts for a, _ in acts}

    def _policy_branches(self, s: Strategy, restart_text: str) -> list[dict]:
        """What the computed policy does with the item right after the main step (shares of attempts)."""
        mdp, u = s.params["mdp"], s.params["usage"]
        labels = self._action_labels(mdp)
        fin: dict = defaultdict(float)
        for p, i in mdp.d0:
            k = mdp.policy[i]
            key = "done" if mdp.acts[i] is None else ("restart" if k is None else mdp.acts[i][k][0].key)
            fin[key] += p * (1 - mdp.R[i])
        restart_v = mdp.restart_cost + mdp.vstart
        kept = self._guaranteed_target(s)
        fractured = s.params.get("frac_target") if s.params.get("fixed") else None
        own = {t.index for t in (kept, fractured) if t is not None}
        other_mask = sum(1 << t.index for t in s.rng_targets if t.index not in own)
        partial = save = 0.0          # restarted items that do have other target mods / what keeping saves
        save_by: dict = defaultdict(float)
        for p, i in mdp.d0:
            k = mdp.policy[i]
            if mdp.acts[i] is None:
                continue
            if k is None:
                if mdp.states[i][1] & other_mask:
                    partial += p
            else:
                save_by[mdp.acts[i][k][0].key] += p * (restart_v - mdp.V[i])
        cur = dict(exalt=CUR("Exalted Orb"), annul=CUR("Orb of Annulment"))
        out = []
        for key, p in sorted(u["first"].items(), key=lambda x: -x[1]):
            if key == "done" or p < 1e-6:
                continue
            if key == "restart":
                if kept is not None:
                    # the essence's own mod is a target: it is on the item, and every new essence brings it back
                    text = M("m.pol.restart_guaranteed", p=_fmt_p(p), then=restart_text, mod=self._td(kept),
                             per=s.params["per"], **cur)
                    if fractured is not None:
                        text = J([text, M("m.pol.also_fractured", mod=self._td(fractured))])
                elif fractured is not None:
                    text = M("m.pol.restart_fractured", p=_fmt_p(p), then=restart_text, mod=self._td(fractured), **cur)
                elif partial > 1e-9:
                    text = M("m.pol.restart_partial", p=_fmt_p(p), then=restart_text, q=_fmt_p(partial / p), **cur)
                else:
                    text = M("m.pol.restart", p=_fmt_p(p), then=restart_text)
                out.append({"text": text, "status": COMPUTED})
                continue
            a = labels[key]
            out.append({"text": M("m.pol.action", p=_fmt_p(p), label=self._act_label(key), f=fin[key] / p,
                                  save=save_by[key] / p), "status": a.status})
        return out

    def _recovery_steps(self, s: Strategy, parent: int) -> list[dict]:
        """Recovery branches of the main step at index `parent`: used only in the item states the computed policy
        assigns them, so each starts with the condition (IF ...) and says how often it comes up."""
        out = []
        essence = s.params.get("base_family") == "essence"
        first = s.params["usage"]["first"]
        for key, d in sorted(s.params["usage"]["actions"].items(), key=lambda x: -x[1]["spend"]):
            if d["uses"] < 1e-4:
                continue
            a = d["action"]
            essence_annul = essence and "annul" in key
            cond = self._recovery_condition(key)
            out.append({
                "kind": "recovery", "branch_of": parent, "condition": cond,
                "title": M("m.recstep.if_title", cond=cond, what=_short(key)),
                "action": M("m.recstep.action", label=self._act_label(key)),
                "detail": M("m.recstep.detail", extra=J([
                    M("m.space", text=M("m.recstep.when", p=_fmt_p(first.get(key, 0.0)))) if first.get(key) else None,
                    M("m.space", text=_LOCK_CHAOS_NOTE) if a.status == APPROX else None,
                    M("m.space", text=_ESSENCE_ANNUL_NOTE) if essence_annul else None])),
                "probability": d["p_progress"],
                "probability_text": M("m.recstep.chance", p=_fmt_p(d["p_progress"])),
                "attempts": d["uses"], "cost": d["spend"],
                "expected_state": M("m.recstep.state", state=self._state(s.rng_targets)),
                "on_success": M("m.recstep.success"),
                "on_fail": [{"text": self._recovery_fail_text(key), "status": COMPUTED}],
                "status": APPROX if essence_annul else a.status,
            })
        return out

    @staticmethod
    def _recovery_condition(key: str) -> Text:
        """The item state in which the policy uses this recovery action (the IF of the branch)."""
        cur = dict(exalt=CUR("Exalted Orb"), annul=CUR("Orb of Annulment"))
        if key.startswith("slam"):
            return M("m.cond.slam", **cur)
        if key == "annul":
            return M("m.cond.annul", **cur)
        kind, side = key.rsplit("_", 1)            # lock_annul_prefix -> ("lock_annul", "prefix")
        return M(f"m.cond.{kind}_{side}", **cur)

    @staticmethod
    def _recovery_fail_text(key: str) -> Text:
        """What 'no progress' means for this particular action."""
        cur = dict(annul=CUR("Orb of Annulment"), exalt=CUR("Exalted Orb"), scour=CUR("Orb of Scouring"),
                   chaos=CUR("Chaos Orb"))
        if "annul" in key:
            return M("m.recfail.annul", meta=M("m.recfail.annul_meta") if key.startswith("lock_") else "", **cur)
        if key.startswith("slam"):
            return M("m.recfail.slam", **cur)
        if key.startswith("lock_scour"):
            return M("m.recfail.scour", **cur)
        return M("m.recfail.chaos", **cur)

    def _act_label(self, key: str) -> Text:
        """Label of a recovery action (the MDP's English labels, as messages with game-data names)."""
        mc = self.rules["metacrafts"]
        cur = dict(exalt=CUR("Exalted Orb"), annul=CUR("Orb of Annulment"), scour=CUR("Orb of Scouring"),
                   chaos=CUR("Chaos Orb"))
        if key in ("slam", "annul"):
            return M(f"m.act.{key}", **cur)
        if key == "restart":
            return M("m.act.restart", **cur)
        if key.startswith("slam_no_"):
            tag = key.rsplit("_", 1)[1]
            return M("m.act.slam_no", meta=G("mod", mc.get(f"no_{tag}"), f"Cannot roll {tag.title()} Modifiers"),
                     **cur)
        for kind in ("lock_annul", "lock_scour", "lock_chaos"):
            if key.startswith(kind + "_"):
                keep = key.rsplit("_", 1)[1]
                other = "suffix" if keep == "prefix" else "prefix"
                rule = "prefixes_locked" if keep == "prefix" else "suffixes_locked"
                meta = G("mod", mc.get(rule), f"{keep.title()}es Cannot Be Changed")
                return M(f"m.act.{kind}", meta=meta, other=other, other_word=M(f"m.sides.{other}"), **cur)
        return key

    def _reroll_detail(self, s: Strategy, fam: str, base: str) -> str:
        """Step detail for the main reroll: guaranteed mod, the mod-count assumption, and the local reason
        for every 'approximate' mark on this step."""
        parts = [base] if base else []
        parts.append(M("m.reroll.modcount"))
        if fam == "harvest":
            parts.append(M("m.approx", text=self._note("harvest_note")))
        if "mdp" in s.params:
            used = [d["action"] for d in s.params["usage"]["actions"].values() if d["uses"] > 1e-6]
            if fam == "essence" and any("annul" in a.key for a in used):
                parts.append(_ESSENCE_ANNUL_NOTE)
            if any(a.status == APPROX for a in used):
                parts.append(M("m.approx", text=_LOCK_CHAOS_NOTE))
        return J(parts, " ")

    def _recovery_ref(self, s: Strategy) -> list[dict]:
        r = s.params.get("recovery_ref")
        if r is not None:
            return [{"text": M("m.recref", cost=r.cost, name=r.name), "status": COMPUTED}]
        note = next((n for n in s.notes if "recovery" in n), None)
        return [{"text": note, "status": COMPUTED}] if note else []

    def _steps(self, ctx: ItemContext, targets, s: Strategy, base_pool) -> list[dict]:
        P = self.prices.get
        rng, bench = s.rng_targets, s.bench
        fam = s.params.get("base_family", s.family)
        rec = "mdp" in s.params
        if fam == "veiled_chaos":                 # the unveiled target is part of the finished item
            targets = list(targets) + [s.params["veiled"]["target"]]
        steps = []
        base = G("base", ctx.base.id, ctx.base.name)
        inf = M("m.base.inf", list=J([N(i.title()) for i in ctx.influences], " + ")) if ctx.influences else ""
        steps.append({
            "title": M("m.base.title"),
            "action": M("m.base.action", base=base, ilvl=ctx.ilvl, inf=inf),
            "expected_state": M("m.base.state", base=base),
            "status": COMPUTED, "on_fail": [],
            "detail": M("m.base.detail"),
        })
        steps += self._eldritch_steps(s)
        if "fracture" in s.params:
            steps += self._fracture_steps(s)
        if fam == "fracture":
            return steps + self._divine_and_done(s, [s.params["frac_target"]])
        if fam == "eldritch":
            steps.append({"title": M("m.done.title"), "action": M("m.done.action"),
                          "expected_state": J([o.label for o in s.params["eldritch"]], "; "),
                          "status": COMPUTED, "on_fail": []})
            return steps
        if fam == "awakener":
            final = list(targets) + [t for _, t, _ in s.params["awakener"]["donors"]]
            return self._awakener_steps(ctx, s) + self._divine_and_done(s, final)
        side_word = (lambda side: M(f"m.side.{side}"))
        if "bench2" in s.params:
            bench_extra = M("m.extra.multimod")
        elif bench and bench.kind == "influence":
            bench_extra = M("m.extra.influence", side=bench.target.affix, side_word=side_word(bench.target.affix),
                            exalt=CUR(s.params["influence"]["exalt"]) if "influence" in s.params
                            else M("m.name.influence_exalt"))
        elif bench and bench.kind == "veiled":
            bench_extra = M("m.extra.aisling", side=bench.target.affix, side_word=side_word(bench.target.affix))
        else:
            bench_extra = (M("m.extra.bench", side=bench.target.affix, side_word=side_word(bench.target.affix))
                           if bench else "")
        if fam == "veiled_chaos":
            vside = s.params["veiled"]["target"].affix
            bench_extra = J([M("m.extra.veiled", side=vside, side_word=side_word(vside)), bench_extra or None], ", ")

        if (fam in ("chaos", "harvest") and not s.params.get("fixed")) or fam == "veiled_chaos":
            steps.append({"title": M("m.alch.title"), "action": M("m.alch.action", alch=CUR("Orb of Alchemy")),
                          "expected_state": M("m.alch.state"), "status": COMPUTED, "on_fail": [],
                          "cost": P("Orb of Alchemy")})
        if fam != "alt":
            what = {
                "chaos": lambda: CUR("Chaos Orb"),
                "veiled_chaos": lambda: CUR(self.rules["veiled_chaos"]),
                "essence": lambda: G("base", s.params["essence"].id, s.params["essence"].name),
                "fossil": lambda: J([J([CUR(f.name) for f in s.params.get("fossils", ())], " + "),
                                     M("m.reroll.resonator", res=CUR(s.params.get("resonator")))]),
                "harvest": lambda: M("m.reroll.harvest", label=s.params.get("label")),
            }[fam]()
            again = (M("m.reroll.again_scour", scour=CUR("Orb of Scouring")) if s.params.get("scour")
                     else M("m.reroll.again"))
            detail = self._reroll_detail(s, fam, M("m.reroll.essence", mod=G("mod", s.params["mod"].id,
                                                                                s.params["mod"].text),
                                                   chaos=CUR("Chaos Orb")) if fam == "essence" else "")
            detail = J([detail, self._influence_repeat_note(s), self._veiled_repeat_note(s)])
            if s.params.get("fixed") and "veiled" not in s.params:
                detail = J([detail, M("m.space", text=self._note("fracture_kept_note"))])
            if rec:
                p_direct = s.params["usage"]["first"].get("done", 0.0)
                steps.append({
                    "title": M("m.reroll.title", what=what), "action": M("m.reroll.action_policy", what=what),
                    "detail": detail, "probability": p_direct, "attempts": 1 / s.p,
                    "cost": s.params["c_attempt"] / s.p,
                    "probability_text": M("m.reroll.chance_policy", direct=_fmt_p(p_direct), finish=_fmt_p(s.p)),
                    "expected_state": M("m.state.rare_with", state=self._state(rng, bench_extra)),
                    "on_success": M("m.step.next"),
                    "on_fail": self._policy_branches(s, again),
                    "status": s.status,
                })
                steps += self._recovery_steps(s, parent=len(steps) - 1)
            else:
                kept = self._guaranteed_target(s)
                fractured = s.params.get("frac_target") if s.params.get("fixed") else None
                if kept is not None and fractured is not None:
                    miss = M("m.reroll.missing_both", again=again, mod=self._td(kept), frac=self._td(fractured))
                elif kept is not None:
                    miss = M("m.reroll.missing_essence", again=again, mod=self._td(kept))
                elif fractured is not None:
                    miss = M("m.reroll.missing_fractured", again=again, mod=self._td(fractured))
                else:
                    miss = M("m.reroll.missing", again=again)
                fail = [{"text": miss, "status": COMPUTED}] + self._recovery_ref(s)
                p_own = s.params["inner"].p if "inner" in s.params else s.p   # this step's own odds
                steps.append({
                    "title": M("m.reroll.title", what=what), "action": M("m.reroll.action", what=what),
                    "detail": detail, "probability": p_own, "attempts": 1 / s.p, "cost": s.cost_rng,
                    "probability_text": (M("m.reroll.chance_inner", p=_fmt_p(p_own)) if "inner" in s.params
                                         else _fmt_p(p_own)),
                    "expected_state": M("m.state.rare_with", state=self._state(rng, bench_extra)),
                    "on_success": M("m.step.next"), "on_fail": fail,
                    "status": s.status if fam != "harvest" else APPROX,
                })
            if fam == "veiled_chaos":
                steps.append(self._unveil_step(s))
        else:
            alt_set, rest, reach = s.params["alt_set"], s.params["rest"], s.params["reach"]
            alt = CUR("Orb of Alteration")
            steps.append({"title": M("m.magic.title"), "action": M("m.magic.action", trans=CUR("Orb of Transmutation")),
                          "expected_state": M("m.magic.state"), "status": COMPUTED, "on_fail": [],
                          "cost": P("Orb of Transmutation")})
            aug_p = s.params["p_aug"]
            steps.append({
                "title": M("m.alt.title", alt=alt),
                "action": M("m.alt.action", alt=alt, targets=J([self._td(t) for t in alt_set], ", ")),
                "probability": s.params["p_alt"], "probability_text": M("m.alt.chance", p=_fmt_p(s.params["p_alt"]), alt=alt),
                "attempts": 1 / s.params["p_alt"], "cost": s.params["e_magic"],
                "expected_state": M("m.state.magic_with", state=self._state(alt_set)),
                "on_success": M("m.step.next"),
                "on_fail": ([{"text": M("m.alt.fail_aug", aug=CUR("Orb of Augmentation"), p=_fmt_p(aug_p), alt=alt),
                              "status": COMPUTED}] if len(alt_set) == 2 and aug_p > 0 else []) +
                           [{"text": M("m.alt.fail_again", alt=alt), "status": COMPUTED}],
                "status": APPROX, "detail": J([self._note("magic_mod_count_note"), self._influence_repeat_note(s)]),
            })
            back = M("m.regal.restart", scour=CUR("Orb of Scouring"))
            if rec:
                p_hit = s.params["p_regal_hit"] or 0.0
                steps.append({
                    "title": CUR("Regal Orb"), "action": M("m.regal.action_policy", regal=CUR("Regal Orb")),
                    "probability": p_hit, "probability_text": M("m.regal.chance", p=_fmt_p(p_hit)),
                    "attempts": 1 / s.p, "cost": P("Regal Orb") / s.p,
                    "expected_state": M("m.regal.state", state=self._state(alt_set)),
                    "on_success": M("m.step.next"),
                    "on_fail": self._policy_branches(s, back),
                    "status": COMPUTED,
                })
                steps += self._recovery_steps(s, parent=len(steps) - 1)
            else:
                for j in range(s.params["steps"]):
                    p_step = (reach[j + 1] / reach[j]) if j + 1 < len(reach) else \
                        (s.params["p_chain"] / reach[j] if reach[j] else 0)
                    orb = "Regal Orb" if j == 0 else "Exalted Orb"
                    goal = (M("m.chain.add", targets=J([self._td(t) for t in rest], ", ")) if rest else
                            M("m.chain.not_block", mod=G("mod", bench.mod.id, bench.mod.text)))
                    done = alt_set + rest[: j + 1]
                    steps.append({
                        "title": CUR(orb), "action": M("m.chain.action", orb=CUR(orb), goal=goal),
                        "probability": p_step, "probability_text": _fmt_p(p_step),
                        "cost": P(orb), "expected_state": M("m.state.rare_with", state=self._state(done)),
                        "on_success": M("m.step.next"),
                        "on_fail": [{"text": M("m.chain.fail", scour=CUR("Orb of Scouring")), "status": COMPUTED}]
                                   + self._recovery_ref(s),
                        "status": COMPUTED,
                    })
        if "bench2" in s.params:
            b2 = s.params["bench2"]
            p_mm = s.params["p_multimod"]
            m1, m2 = G("mod", bench.mod.id, bench.mod.text), G("mod", b2.mod.id, b2.mod.text)
            if p_mm > 1e-9:
                action = M("m.mm.both", p=_fmt_p(p_mm), cost=s.params["multimod_cost"], a=m1, b=m2)
            else:
                action = M("m.mm.either", a=m1, b=m2)
            inferred = ItemContext.bench_inferred(bench.mod) or ItemContext.bench_inferred(b2.mod)
            steps.append({
                "title": M("m.mm.title_mm") if p_mm > 1e-9 else M("m.bench.title"),
                "action": action,
                "detail": M("m.mm.detail", extra=M("m.space", text=_BENCH_INFERRED_NOTE) if inferred else ""),
                "probability": 1.0, "probability_text": M("m.det"),
                "cost": s.cost_bench, "expected_state": M("m.state.rare_with", state=self._state(targets)),
                "on_fail": [], "status": APPROX if inferred else COMPUTED,
            })
        elif bench and bench.kind == "influence":
            final = list(targets) + [bench.target]
            steps.append(self._influence_step(s, self._state(final)))
            return steps + self._divine_and_done(s, final)
        elif bench and bench.kind == "veiled":
            final = list(targets) + [bench.target]
            steps += self._aisling_steps(s)
            return steps + self._divine_and_done(s, final)
        elif bench:
            steps.append({
                "title": M("m.bench.title"),
                "action": M("m.bench.action", mod=G("mod", bench.mod.id, bench.mod.text), master=N(bench.craft.master)),
                "detail": M("m.bench.detail",
                            costs=J([M("m.bench.cost_item", cur=CUR(name), n=count) for name, count in bench.craft.cost], ", "),
                            value=M("m.bench.value", p=_fmt_p(bench.value_chance)) if bench.value_chance < 1 else "",
                            inferred=M("m.bench.inferred", note=_BENCH_INFERRED_NOTE)
                            if ItemContext.bench_inferred(bench.mod) else ""),
                "cost": bench.cost, "expected_state": M("m.state.rare_with", state=self._state(targets)),
                "on_fail": [], "status": APPROX if ItemContext.bench_inferred(bench.mod) else COMPUTED,
            })
        if s.divines > 0:
            q = s.params["value_q"]
            div = CUR("Divine Orb")
            steps.append({
                "title": M("m.divine.optional_title", divine=div), "optional": True,
                "action": M("m.divine.action", divine=div),
                "probability": q, "probability_text": M("m.divine.chance", p=_fmt_p(q), divine=div),
                "attempts": s.divines, "cost": s.cost_divine,
                "detail": J([M("m.divine.detail_low"), M("m.divine.optional_note", p=_fmt_p(q))], " "),
                "expected_state": M("m.finished", state=self._state(targets)), "on_fail": [
                    {"text": M("m.divine.fail", divine=div), "status": COMPUTED}],
                "status": COMPUTED,
            })
        steps.append({"title": M("m.done.title"), "action": M("m.done.action"), "expected_state": self._state(targets),
                      "status": COMPUTED, "on_fail": []})
        return steps

    @staticmethod
    def _influence_repeat_note(s: Strategy) -> str:
        if "inner" not in s.params or "influence" not in s.params:
            return ""
        i = s.params["influence"]
        return M("m.inf.repeat", exalt=CUR(i["exalt"]), n=1 / i["p"])

    def _divine_and_done(self, s: Strategy, final_targets) -> list[dict]:
        out = []
        if s.divines > 0:
            q = s.params["value_q"]
            div = CUR("Divine Orb")
            out.append({
                "title": M("m.divine.optional_title", divine=div), "optional": True,
                "action": M("m.divine.action", divine=div),
                "probability": q, "probability_text": M("m.divine.chance", p=_fmt_p(q), divine=div),
                "attempts": s.divines, "cost": s.cost_divine,
                "detail": J([M("m.divine.detail"), M("m.divine.optional_note", p=_fmt_p(q))], " "),
                "expected_state": M("m.finished", state=self._state(final_targets)),
                "on_fail": [{"text": M("m.divine.fail", divine=div), "status": COMPUTED}],
                "status": COMPUTED,
            })
        out.append({"title": M("m.done.title"), "action": M("m.done.action"), "expected_state": self._state(final_targets),
                    "status": COMPUTED, "on_fail": []})
        return out

    # ============================================================ recover a given item
    def recover(self, base_id: str, ilvl: int, influences, target_specs: list[dict], have_ids: list[str],
                full_sims: bool = False, lang: str | None = "en") -> dict:
        """See _recover; `lang` as in plan()."""
        with self._run():
            result = self._recover(base_id, ilvl, influences, target_specs, have_ids, full_sims)
        return result if lang is None else render(result, lang)

    def _recover(self, base_id: str, ilvl: int, influences, target_specs: list[dict], have_ids: list[str],
                 full_sims: bool = False) -> dict:
        """'I already have this rare - what now?' Same RecoveryMDP; restarting = paying for the best fresh plan.
        The fresh plan only supplies the calculated 'start over' cost (simulation never changes a plan's cost
        or ranking), so it is not simulated unless full_sims is set. The recovery options themselves are
        calculated by the MDP and were never simulated."""
        if any(s.get("eldritch") for s in target_specs):
            raise ValueError(M("m.rec.err_eldritch"))
        if any(s.get("veiled") for s in target_specs):
            raise ValueError(M("m.rec.err_veiled", vc=CUR("Veiled Chaos Orb")))
        base = self.data.bases.get(base_id) or self.data.base_by_name(base_id)
        ctx = ItemContext(self.data, self.rules, base, int(ilvl), influences)
        # displayed -> stored units for this mode's own targets; target_specs stay displayed for self.plan() below
        targets = ctx.make_targets(self._specs_to_stored(ctx, target_specs))
        self._register_mods([e.mod for e in ctx.base_entries] + [m for _, m in ctx.essence_options]
                            + [m for _, m in ctx.bench_options])
        L = ctx.limits
        have = []
        for mid in have_ids:
            m = self.data.mods.get(mid)
            if m is None:
                raise ValueError(M("m.rec.err_mod", mid=mid))
            have.append(m)
        for affix in ("prefix", "suffix"):
            if sum(m.affix == affix for m in have) > L[affix]:
                raise ValueError(M("m.rec.err_count", n=L[affix], side=affix, side_word=M(f"m.sides.{affix}")))
        groups = [m.group for m in have]
        if len(groups) != len(set(groups)):
            raise ValueError(M("m.rec.err_group"))

        sims = self.sims
        if not full_sims:
            self.sims = 0
        restart_note = None
        try:
            try:
                fresh = self.plan(base.id, ilvl, influences, target_specs, lang=None)
            except Infeasible:
                # the item already has its fractured mod (e.g. a fractured, influenced item - a Fracturing Orb would
                # not be allowed on it): price "start over" as rolling that mod normally - an upper bound, since
                # Scouring keeps a fractured mod - instead of failing the whole recovery
                if not any(s.get("fractured") for s in target_specs):
                    raise
                plain = [{k: v for k, v in s.items() if k != "fractured"} for s in target_specs]
                fresh = self.plan(base.id, ilvl, influences, plain, lang=None)
                restart_note = M("m.rec.restart_unfractured")
        finally:
            self.sims = sims
        fresh_best = fresh["strategies"][0]
        restart_total = fresh_best["expected_cost"]
        base_pool = ctx.pool()
        costs = self._mdp_costs(ctx.base.item_class)
        P = self.prices.get
        best = None
        for bench in [None] + [b for b in (self._bench_choice(ctx, t) for t in targets) if b]:
            bt = bench.target if bench else None
            rng = [t for t in targets if bt is None or t.index != bt.index]
            gp = GroupedPool(base_pool, targets, (), bench.groups if bench else (), track=tuple(have))
            s0 = gp.state_of(have)
            _, divs = self._divine(rng, base_pool, tuple(have))
            div_cost = divs * P("Divine Orb") if divs > 1e-6 else 0.0
            req = _mask(rng)

            def terminal(st, bench=bench, bt=bt):      # Divines for the numbers are optional (div_cost: reported)
                return bench.cost if bench and not st[1] >> bt.index & 1 else 0.0

            def done(st, gp=gp, req=req, bt=bt):
                return gp.is_success(st, req, bt, L)

            # restarting from this rare: Scouring only when the fresh plan starts from a Normal item
            restart_cost = P("Orb of Scouring") if fresh_best.get("starts_from_normal") else 0.0
            mdp = RecoveryMDP(gp, L, self.rare_n, costs, restart_cost, done, terminal)
            try:
                mdp.solve_constant([s0], restart_total)
            except MdpTooLarge:
                continue
            v = mdp.value(s0)
            if best is None or v < best[0]:
                best = (v, mdp, gp, s0, bench, rng)
        if best is None:
            raise ValueError(M("m.rec.err_large"))
        v, mdp, gp, s0, bench, rng = best
        q_have, divs_have = self._divine(rng, base_pool, tuple(have))     # optional Divines, reported separately
        divs_have = divs_have if divs_have > 1e-6 else 0.0

        def describe(st):
            return self._describe_state(gp, targets, st)

        options = []
        for o in ([] if mdp.done(s0) else mdp.options_at(s0)):
            outs = sorted(o.pop("outcomes"), key=lambda x: -x[0])
            o["label"] = self._act_label(o["key"])
            o["outcomes"] = [{
                "p": p, "state": M("m.rec.start_over") if j < 0 else describe(mdp.states[j]),
                "next": M("m.rec.start_over") if j < 0 else self._next_text(mdp, mdp.states[j]),
                "better": j >= 0 and mdp.V[j] < v - 1e-9,
            } for p, j in outs]
            options.append(o)
        return {
            "item": {"base": base.name, "base_id": base.id, "item_class": base.item_class, "ilvl": ctx.ilvl,
                     "influences": list(ctx.influences)},
            "targets": [self._td(t) for t in targets],
            "have": [{"id": m.id, "text": m.text, "affix": m.affix} for m in have],
            "state": describe(s0),
            "already_done": mdp.done(s0),
            "bench": bench.mod.text if bench else None,
            "expected_cost_from_here": v,
            "optional_divine": ({"cost": divs_have * P("Divine Orb"), "divines": divs_have, "p_values_met": q_have,
                                 "total_with": v + divs_have * P("Divine Orb")} if divs_have else None),
            "fresh_plan": {"name": fresh_best["name"], "expected_cost": restart_total, "basis": "calculated",
                           "simulation": fresh_best["sim"] if full_sims else None},
            "options_basis": "calculated",
            "options": options,
            "steps": self._recover_steps(have, targets, mdp, s0, bench, options, fresh_best, describe),
            "warnings": fresh["warnings"] + ([restart_note] if restart_note else []), "prices": fresh["prices"],
            "features": feature_rows(self.rules),
        }

    # ------------------------------------------------------- game names inside explanations
    def _note(self, name: str) -> Text:
        """A rules note (mechanics.json `name`) as a message; currency names inside it are game-data parameters."""
        return rule_note(name)

    # ------------------------------------------------------- game names inside explanations
    def _register_mods(self, mods) -> None:
        """Remember which mods give each stat line, so target labels in explanations can be localized by mod id."""
        for m in mods:
            if m is not None and getattr(m, "text", None) and getattr(m, "signature", None):
                self._gref.setdefault(m.signature, {}).setdefault(generic_text(m.text), (m.id, m.text))

    def _fossil_id(self, name: str):
        return next((x.id for x in self.data.fossils if x.name == name), None)

    @staticmethod
    def _fossil_name(combo) -> Text:
        """'Fossil spam: Pristine + Frigid' - fossil names are game data (localized by id; English keeps the
        short form without ' Fossil')."""
        return M("m.strategy.fossil", names=J([G("base", f.id, f.name.replace(" Fossil", "")) for f in combo], " + "))

    @staticmethod
    def _fam_label(fam):
        """A Family's label as a game-data parameter (the mod it was made from, localized by id)."""
        src = next((t for t in fam.tiers if t.get("text") and generic_text(t["text"]) == fam.label),
                   fam.tiers[0] if fam.tiers else None)
        return GL(src["mod_id"], fam.label, src["text"]) if src else fam.label

    def _tl(self, t: Target):
        """A target's mod label as a game-data parameter (localized by the id of a mod with exactly that label)."""
        ref = self._gref.get(t.signature, {}).get(t.label)
        return GL(ref[0], t.label, ref[1]) if ref else t.label

    def _td(self, t: Target):
        """Target description: localized label (+ a translated suffix such as ' (highest tier)') + the threshold."""
        return J([self._tl(t), self._tsuffix.get((t.signature, t.index)),
                  None if t.min_value is None else f" (>= {t.shown_min:g})"])

    @staticmethod
    def _game_ref(label: str, mods, t: Target | None = None) -> dict:
        """Display hook for game-data localization (no effect on planning): the id and English text of a mod
        behind a target, and the English label its description starts with. A mod whose generic text is exactly
        the label is preferred, so the localized label can be built the same way (number-to-# step)."""
        cands = [m for m in mods if m is not None and getattr(m, "text", None) and (t is None or t.satisfied_by(m))]
        m = next((m for m in cands if generic_text(m.text) == label), cands[0] if cands else None)
        return {"game_id": m.id, "game_label": label, "game_text": m.text} if m is not None else {}

    def _describe_state(self, gp: GroupedPool, targets, st) -> Text:
        if st == RESTART:
            return M("m.rec.start_over")
        np_, ns_ = gp.counts(st)
        has = [self._td(t) for t in targets if st[1] >> t.index & 1]
        miss = [self._tl(t) for t in targets if not st[1] >> t.index & 1]
        return M("m.rec.state", np=np_, ns=ns_, has=J(has, ", ") if has else M("m.rec.no_targets"),
                 miss=M("m.rec.missing", list=J(miss, ", ")) if miss else "")

    @staticmethod
    def _next_text(mdp: RecoveryMDP, st) -> Text:
        if mdp.done(st):
            return M("m.rec.next_finish")
        a = mdp.action_at(st)
        return M("m.rec.next", what=_short(a.key) if a else M("m.rec.start_over"))

    def _recover_steps(self, have, targets, mdp, s0, bench, options, fresh_best, describe) -> list[dict]:
        scour = CUR("Orb of Scouring")
        steps = [{"title": M("m.rec.current.title"),
                  "action": M("m.rec.current.action", mods=J([M("m.rec.mod", affix=M(f"m.side.{m.affix}"),
                                                                text=G("mod", m.id, m.text)) for m in have], "; ")),
                  "expected_state": describe(s0), "status": COMPUTED, "on_fail": []}]
        finish = (M("m.rec.finish_bench", mod=G("mod", bench.mod.id, bench.mod.text)) if bench
                  else M("m.rec.finish_divine"))
        start = M("m.rec.start_scour" if fresh_best.get("starts_from_normal") else "m.rec.start_direct",
                  scour=scour, plan=fresh_best["name"])
        if mdp.done(s0):
            steps.append({"title": M("m.rec.finish.title"), "action": M("m.rec.finish.action"),
                          "probability": 1.0, "probability_text": "100%", "cost": mdp.value(s0),
                          "expected_state": M("m.state.rare_with", state=self._state(targets)), "on_fail": [],
                          "status": COMPUTED})
        elif options[0]["key"] == "restart":
            o = options[0]
            steps.append({"title": M("m.rec.restart.title"),
                          "action": M("m.rec.restart.action", state=describe(s0), start=start),
                          "probability": 0.0, "probability_text": "-", "cost": o["expected_total"],
                          "expected_state": M("m.rec.restart.state"), "on_fail": [], "status": COMPUTED,
                          "detail": M("m.rec.restart.detail")})
        else:
            o = options[0]
            good = max((x for x in o["outcomes"] if x["better"]), key=lambda x: x["p"], default=None)
            steps.append({
                "title": M("m.recstep.title", what=_short(o["key"])),
                "action": M("m.recstep.action", label=o["label"]),
                "detail": M("m.rec.option.detail", p=_fmt_p(o["p_keep_targets"]),
                            finish=M("m.rec.option.finish", cost=o["cost_to_finish_this_item"])
                            if o["cost_to_finish_this_item"] is not None else ""),
                "probability": o["p_progress"], "probability_text": M("m.rec.option.chance", p=_fmt_p(o["p_progress"])),
                "cost": o["action_cost"],
                "expected_state": good["state"] if good else describe(s0),
                "on_success": good["next"] if good else "",
                "on_fail": [{"text": M("m.rec.outcome", p=_fmt_p(x["p"]), state=x["state"], next=x["next"]),
                             "status": COMPUTED} for x in o["outcomes"] if not x["better"]],
                "status": o["status"],
            })
            steps.append({
                "title": M("m.rec.continue.title"),
                "action": M("m.rec.continue.action", exalt=CUR("Exalted Orb"), annul=CUR("Orb of Annulment")),
                "probability": o["p_finish_without_restart"],
                "probability_text": M("m.rec.continue.chance", p=_fmt_p(o["p_finish_without_restart"])),
                "cost": o["continuation_cost"], "detail": M("m.rec.continue.detail", finish=finish),
                "expected_state": M("m.state.rare_with", state=self._state(targets)),
                "on_fail": [{"text": M("m.rec.continue.fail", start=start, cost=fresh_best["expected_cost"]),
                             "status": COMPUTED}],
                "status": o["status"],
            })
        steps.append({"title": M("m.done.title"), "action": M("m.done.action"), "expected_state": self._state(targets),
                      "status": COMPUTED, "on_fail": []})
        return steps
