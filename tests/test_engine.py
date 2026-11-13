"""Run: python -m unittest discover -s tests -v"""
import json
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from poecraft import config  # noqa: E402
from poecraft.data.repository import Mod, Stat  # noqa: E402
from poecraft.engine.analytic import GroupedPool  # noqa: E402
from poecraft.engine.context import PoolEntry, Target  # noqa: E402
from poecraft.engine.simulate import Simulator, satisfies  # noqa: E402

LIMITS = {"prefix": 3, "suffix": 3}


def mod(mid, affix, stat, lo=1, hi=10, group=None, tags=()):
    return Mod(mid, mid, "item", affix, mid, 1, False, f"+({lo}-{hi}) {stat}", (group or mid,), tuple(tags),
               (("default", 1000),), (), (Stat(stat, lo, hi),))


def target(i, m, mn=None):
    return Target(i, m.signature, m.stats[0].id, mn, m.text, m.affix)


class SyntheticPool(unittest.TestCase):
    def test_single_pick_is_exact(self):
        a = mod("A", "prefix", "a")
        entries = [PoolEntry(a, 100, 1), PoolEntry(mod("B", "prefix", "b"), 900, 1),
                   PoolEntry(mod("C", "suffix", "c"), 1000, 1)]
        gp = GroupedPool(entries, [target(0, a)])
        final = gp.roll({1: 1.0}, LIMITS)
        self.assertAlmostEqual(gp.success(final, 1, None, LIMITS), 0.05, places=12)

    def test_value_threshold_and_tiers(self):
        t1 = mod("L1", "prefix", "life", 90, 99, group="Life")
        t2 = mod("L2", "prefix", "life", 70, 79, group="Life")
        t = target(0, t1, 95)
        self.assertTrue(t.satisfied_by(t1))
        self.assertFalse(t.satisfied_by(t2))         # tier cap below the threshold
        self.assertAlmostEqual(t.value_chance(t1), 5 / 10)

    def test_analytic_matches_simulation(self):
        entries = []
        targets = []
        for i in range(12):
            for affix in ("prefix", "suffix"):
                m = mod(f"{affix}{i}", affix, f"{affix}{i}", tags=("x",) if i % 3 == 0 else ())
                entries.append(PoolEntry(m, 200 + 150 * (i % 4), 1))
        targets = [target(0, entries[0].mod), target(1, entries[3].mod)]
        gp = GroupedPool(entries, targets)
        n_dist = {4: 8 / 12, 5: 3 / 12, 6: 1 / 12}
        p = gp.success(gp.roll(n_dist, LIMITS), 0b11, None, LIMITS)
        sim = Simulator(entries, seed=7)
        n = 40000
        hits = sum(satisfies(sim.roll(n_dist, LIMITS), targets, None, LIMITS) for _ in range(n))
        self.assertAlmostEqual(p, hits / n, delta=max(0.01, 0.1 * p))

    def test_annul_uniform_and_respects_lock(self):
        a, b = mod("A", "prefix", "a"), mod("B", "suffix", "b")
        entries = [PoolEntry(a, 100, 1), PoolEntry(b, 100, 1), PoolEntry(mod("J", "suffix", "j"), 100, 1),
                   PoolEntry(mod("K", "suffix", "k"), 100, 1)]
        gp = GroupedPool(entries, [target(0, a), target(1, b)])
        s = gp.state_of([a, b, entries[2].mod])            # 2 good + 1 junk (aggregated "other" suffix)
        n, outs = gp.annul(s)
        self.assertEqual(n, 3)
        self.assertTrue(all(abs(p - 1 / 3) < 1e-12 for p, _ in outs))
        n, outs = gp.annul(s, locked="prefix", extra=1)      # Prefixes Cannot Be Changed: A is safe
        self.assertEqual(n, 3)                               # B, junk, the metacraft itself
        self.assertTrue(all(st[1] & 1 for _, st in outs))    # target A survives every outcome
        self.assertEqual(gp.scour_side(s, "prefix")[1], 1)   # locked Scour keeps A, wipes B and junk

    def test_group_exclusion(self):
        # two mods share a group: at most one of them can be on the item
        a = mod("A1", "prefix", "a", group="G")
        b = mod("A2", "prefix", "b", group="G")
        entries = [PoolEntry(a, 500, 1), PoolEntry(b, 500, 1), PoolEntry(mod("C", "suffix", "c"), 500, 1)]
        gp = GroupedPool(entries, [target(0, a), target(1, b)])
        final = gp.roll({3: 1.0}, LIMITS)
        self.assertEqual(gp.success(final, 0b11, None, LIMITS), 0.0)


class LivePrices(unittest.TestCase):
    BODY = {"core": {"primary": "chaos", "secondary": "divine", "rates": {"divine": 0.004},
                     "items": [{"id": "chaos", "name": "Chaos Orb"}]},
            "lines": [{"id": "chaos", "primaryValue": 1}, {"id": "alt", "primaryValue": 0.1},
                      {"id": "ghost", "primaryValue": 9}],
            "items": [{"id": "alt", "name": "Orb of Alteration"}]}

    def test_parse_overview(self):
        from poecraft.data.ninja import parse_overview
        self.assertEqual(parse_overview(self.BODY), {"Chaos Orb": 1.0, "Orb of Alteration": 0.1})

    def test_parse_non_chaos_primary(self):
        from poecraft.data.ninja import NinjaError, parse_overview
        body = {"core": {"primary": "divine", "rates": {"chaos": 250}}, "lines": [{"id": "ex", "primaryValue": 0.02}],
                "items": [{"id": "ex", "name": "Exalted Orb"}]}
        self.assertAlmostEqual(parse_overview(body)["Exalted Orb"], 5.0)
        with self.assertRaises(NinjaError):
            parse_overview({"core": {"primary": "divine", "rates": {}}, "lines": []})

    def test_status_states_and_fallback(self):
        import json
        import tempfile
        import time
        from poecraft.data.ninja import load_status
        from poecraft.engine.prices import Prices
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "live.json"
            self.assertEqual(load_status(p)["state"], "unavailable")          # never fetched
            snap = {"_meta": {"league": "L", "fetched_at": now, "source": "https://poe.ninja/poe1/api/economy", "last_attempt": {"ok": True, "selection_verified": True}},
                    "prices": {"Divine Orb": 300}}
            p.write_text(json.dumps(snap))
            self.assertEqual(load_status(p)["state"], "live")
            self.assertEqual(Prices(live_path=p).get("Divine Orb"), 300)
            snap["_meta"]["last_attempt"] = {"selection_verified": True, "ok": False, "error": "down", "at": now}
            p.write_text(json.dumps(snap))
            self.assertEqual(load_status(p)["state"], "stale")
            snap["_meta"]["fetched_at"] = "2020-01-01T00:00:00Z"                 # too old -> estimates
            p.write_text(json.dumps(snap))
            st = load_status(p)
            self.assertEqual(st["state"], "unavailable")
            self.assertEqual(Prices(live_path=p).get("Divine Orb"), Prices(use_live=False).get("Divine Orb"))


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class RealData(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.context import ItemContext
        cls.data, cls.rules = get_data(), get_rules()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.ctx = ItemContext(cls.data, cls.rules, cls.base, 86, [])

    def test_database_contents(self):
        self.assertGreater(len(self.data.bases), 500)
        self.assertGreater(len(self.data.mods), 3000)
        self.assertGreater(len(self.data.essences), 50)
        self.assertGreater(len(self.data.bench_crafts), 300)

    def test_es_base_has_no_armour_rolls_or_crafts(self):
        fams = self.ctx.families()
        labels = {f.label for f in fams.values()}
        self.assertIn("+# to maximum Energy Shield", labels)
        self.assertNotIn("#% increased Armour", labels)

    def test_plan_end_to_end(self):
        from poecraft.engine.planner import Planner
        fams = {f.label: f for f in self.ctx.families().values()}
        specs = [{"signature": fams["+# to maximum Life"].signature, "min": 100},
                 {"signature": fams["+#% to Cold Resistance"].signature, "min": 30}]
        result = Planner(sims=3000).plan(self.base.id, 86, [], specs)
        self.assertTrue(result["strategies"])
        best = result["strategies"][0]
        self.assertGreater(best["expected_cost"], 0)
        self.assertGreater(len(best["steps"]), 2)
        costs = [s["expected_cost"] for s in result["strategies"]]
        self.assertEqual(costs, sorted(costs))

    def test_impossible_target_rejected(self):
        from poecraft.engine.planner import Planner
        fams = {f.label: f for f in self.ctx.families().values()}
        specs = [{"signature": fams["+# to maximum Life"].signature, "min": 100000}]
        with self.assertRaises(ValueError):
            Planner(sims=0).plan(self.base.id, 86, [], specs)


class Selection(unittest.TestCase):
    """The recommendation rule itself, independent of game data."""

    @staticmethod
    def cand(name, cost, sd, apps=1.0):
        from types import SimpleNamespace
        return SimpleNamespace(name=name, params={"metrics": {"cost": cost, "sd": sd, "risk": cost + 2 * sd,
                                                              "apps": apps}})

    def ranked(self, objective, cands):
        from poecraft.engine.planner import OBJECTIVES
        return sorted(cands, key=lambda c: OBJECTIVES[objective]["score"](c.params["metrics"]))

    def test_min_variance_prefers_the_steadier_plan(self):
        from poecraft.engine.planner import select_recommended
        # cheap on average but expensive when recovery goes badly vs slightly dearer but steady
        swingy, steady = self.cand("swingy", 100, 80), self.cand("steady", 110, 20)
        agree = lambda c: (True, True, "")
        self.assertIs(select_recommended(self.ranked("min_cost", [steady, swingy]), agree)[0], swingy)
        self.assertIs(select_recommended(self.ranked("min_variance", [swingy, steady]), agree)[0], steady)

    def test_disagreeing_candidate_is_demoted_with_reason(self):
        from poecraft.engine.planner import select_recommended
        a, b = self.cand("a", 50, 10), self.cand("b", 60, 10)
        verify = lambda c: (True, c is not a, "fast and simulated disagree")
        winner, demoted, verified = select_recommended(self.ranked("min_cost", [a, b]), verify)
        self.assertIs(winner, b)
        self.assertEqual(demoted, [(a, "fast and simulated disagree")])
        self.assertTrue(verified)

    def test_simulation_off_returns_top_unverified(self):
        from poecraft.engine.planner import select_recommended
        a, b = self.cand("a", 50, 10), self.cand("b", 60, 10)
        winner, demoted, verified = select_recommended([a, b], lambda c: (False, True, "off"))
        self.assertIs(winner, a)
        self.assertFalse(verified)


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class Feasibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules = get_data(), get_rules()
        cls.planner = Planner(prices=Prices(use_live=False), sims=0)

    def test_mutually_exclusive_targets_get_no_plan(self):
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Infeasible
        base = self.data.base_by_name("Vaal Regalia")
        fams = {f.label: f.signature for f in ItemContext(self.data, self.rules, base, 86, []).families().values()}
        a, b = "+# to maximum Energy Shield / +# to maximum Life", "#% increased Energy Shield / +# to maximum Life"
        specs = [{"signature": fams[a]}, {"signature": fams[b]},
                 {"signature": fams["+#% to Cold Resistance"], "min": 36}]
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(base.id, 86, [], specs)        # raises instead of returning any plan
        report = cm.exception.report
        self.assertIn("No supported strategy reaches these targets", str(cm.exception))
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertIn("BaseLocalDefencesAndLife", report["conflicts"][0])
        reachable = {r["target"] for r in report["reachable"]}
        self.assertEqual(reachable, {a, b, "+#% to Cold Resistance (>= 36)"})   # each is reachable on its own
        self.assertEqual(report["unreachable"], [])

    def test_unreachable_mod_says_which_influence_it_needs(self):
        from poecraft.engine.planner import Infeasible
        base = self.data.base_by_name("Stygian Vise")
        sig = self.data.mods["MaximumLifeInfluence2"].signature          # % maximum Life: influence-only on belts
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(base.id, 84, [], [{"signature": sig}])
        reason = cm.exception.report["unreachable"][0]["reason"]
        self.assertIn("Hunter", reason)
        self.assertIn("influence", reason)


@unittest.skipUnless((config.RAW_DIR / "mods.json").exists() and config.DB_PATH.exists(),
                     "raw RePoE data / database missing - run `python -m poecraft update`")
class Eldritch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.eldritch import get_eldritch
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine import eldritch
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules, cls.eld = get_data(), get_rules(), get_eldritch()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.ctx = ItemContext(cls.data, cls.rules, cls.base, 86, [])
        cls.efams = {(f.side, f.label): f for f in eldritch.all_families(cls.eld).values()}
        cls.xfams = {f.label: f.signature for f in cls.ctx.families().values()}
        cls.planner = Planner(prices=Prices(use_live=False), sims=300)

    def efam(self, side, label):
        return self.efams[(side, label)]

    def test_tiers_come_from_the_data(self):
        mods = {m.id: m for m in self.eld.mods}
        t6, t1 = mods["IncreasedAttackSpeedEldritchImplicit1"], mods["IncreasedAttackSpeedEldritchImplicit6"]
        self.assertEqual((t6.tier, t1.tier), (6, 1))                       # no_tier_6 tag -> tier 6 (weakest)
        self.assertLess(t6.stats[0].max, t1.stats[0].max)
        self.assertEqual({c.side for c in self.eld.currencies if "Ember" in c.name}, {"exarch"})
        self.assertEqual({c.side for c in self.eld.currencies if "Ichor" in c.name}, {"eater"})

    def test_eldritch_implicit_only_plan(self):
        f = self.efam("exarch", "+#% to Critical Strike Multiplier for Spell Damage")
        result = self.planner.plan(self.base.id, 86, [], [{"eldritch": "exarch", "signature": f.key, "min": 26}])
        best = result["strategies"][0]
        self.assertEqual(best["family"], "eldritch")
        self.assertIn("Exceptional Eldritch Ember", best["name"])        # 26+ needs tier 3 = Exceptional
        step = next(s for s in best["steps"] if s["title"].startswith("Eldritch implicit"))
        # odds = this implicit's share of the tier-3 Searing Exarch pool for Body Armour, from spawn weights
        pool = [m for m in self.eld.mods if m.side == "exarch" and m.tier == 3 and m.weight(self.ctx.tags) > 0]
        share = sum(m.weight(self.ctx.tags) for m in pool if m.key == f.key) / sum(m.weight(self.ctx.tags) for m in pool)
        self.assertAlmostEqual(step["probability"], share, places=12)
        self.assertAlmostEqual(best["expected_cost"], best["cost_breakdown"]["eldritch"])
        self.assertEqual(best["status"], "approximate")
        self.assertIn("currency-to-tier", step["detail"])                # the approximation is explained locally

    def test_eldritch_plus_explicit_combines_both(self):
        f = self.efam("eater", "#% of Physical Damage from Hits taken as Fire Damage")
        specs = [{"signature": self.xfams["+# to maximum Life"], "min": 140},
                 {"signature": self.xfams["+#% to Cold Resistance"], "min": 36},
                 {"eldritch": "eater", "signature": f.key, "min": 11}]
        result = self.planner.plan(self.base.id, 86, [], specs)
        rec = next(r for r in result["comparison"] if r["why"] == "RECOMMENDED")
        self.assertIn("+ Eldritch implicit (", rec["name"])
        pure = [r for r in result["comparison"] if "does not reach the Eldritch implicit target" in r["flags"]]
        combined = [r for r in result["comparison"] if "+ Eldritch implicit (" in r["name"]]
        self.assertTrue(pure and combined)                                 # both kinds listed side by side
        eld_cost = result["strategies"][0]["cost_breakdown"]["eldritch"]
        twin = next(r for r in pure if r["name"] == rec["name"].split(" + Eldritch implicit (")[0])
        self.assertAlmostEqual(rec["cost"], twin["cost"] + eld_cost)      # Eldritch cost simply adds
        self.assertTrue(all(not r["why"] == "RECOMMENDED" for r in pure))

    def test_eldritch_on_a_base_that_cannot_have_it(self):
        from poecraft.engine.planner import Infeasible
        ring = self.data.base_by_name("Two-Stone Ring")
        f = self.efam("exarch", "+#% to maximum Cold Resistance")
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(ring.id, 86, [], [{"eldritch": "exarch", "signature": f.key, "min": 2}])
        reason = cm.exception.report["unreachable"][0]["reason"]
        self.assertIn("Ring cannot have Eldritch implicits", reason)
        self.assertIn("Body Armour", reason)

    def test_offline_eldritch_plan_uses_config_estimate_not_unknown(self):
        from poecraft.engine.prices import Prices
        prices = Prices(use_live=False)
        f = self.efam("exarch", "+#% to Critical Strike Multiplier for Spell Damage")
        result = self.planner.plan(self.base.id, 86, [], [{"eldritch": "exarch", "signature": f.key, "min": 26}])
        best = result["strategies"][0]
        step = next(s for s in best["steps"] if s["title"].startswith("Eldritch implicit"))
        price = prices.table["Exceptional Eldritch Ember"]
        self.assertGreater(price, prices.table["unknown"])
        self.assertAlmostEqual(best["cost_breakdown"]["eldritch"], price / step["probability"])
        row = next(k for k in result["prices"]["key_prices"] if k["name"] == "Exceptional Eldritch Ember")
        self.assertEqual((row["chaos"], row["source"]), (price, "estimated"))
        self.assertFalse(any(w.startswith("No price configured") for w in result["warnings"]))
        self.assertTrue(any(w.startswith("Price estimate: Exceptional Eldritch Ember has no live price")
                            for w in result["warnings"]))

    def test_amulet_reason_names_the_currency_rule(self):
        from poecraft.engine.planner import Infeasible
        amulet = self.data.base_by_name("Onyx Amulet")
        f = self.efam("exarch", "#% increased Critical Strike Chance for Attacks")
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(amulet.id, 86, [], [{"eldritch": "exarch", "signature": f.key, "min": 25}])
        self.assertIn("Eldritch currency does not apply to amulets (per currency descriptions)",
                      cm.exception.report["unreachable"][0]["reason"])

    def test_worked_example_ignores_eldritch(self):
        specs = [{"signature": self.xfams["+# to maximum Life"], "min": 140},
                 {"signature": self.xfams["+# to maximum Energy Shield"], "min": 50},
                 {"signature": self.xfams["+#% to Cold Resistance"], "min": 36},
                 {"signature": self.xfams["+#% to Lightning Resistance"], "min": 36}]
        result = self.planner.plan(self.base.id, 86, [], specs)
        self.assertNotIn("Eldritch", result["recommended"])
        self.assertTrue(all(s["cost_breakdown"]["eldritch"] == 0 for s in result["strategies"]))
        self.assertFalse(any("Eldritch" in r["name"] or any("Eldritch" in f for f in r["flags"])
                             for r in result["comparison"]))


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class Influence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine import influence
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules, cls.inf = get_data(), get_rules(), influence
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.ctx = {i: influence.influence_context(cls.data, cls.rules, cls.base, 86, (i,)) for i in ("hunter", "warlord")}
        cls.xfams = {f.label: f.signature for f in ItemContext(cls.data, cls.rules, cls.base, 86, []).families().values()}
        cls.planner = Planner(prices=Prices(use_live=False), sims=1000)

    def fam(self, inf, label):
        return {f.label: f.signature for f in self.ctx[inf].families().values()}[label]

    def test_exalt_names_exist_in_the_data(self):
        names = set(self.data.currency_names.values())
        for exalt in list(self.rules["influence_exalts"].values()) + [self.rules["awakener_orb"],
                                                                     self.rules["dominance_orb"]]:
            self.assertIn(exalt, names)

    def test_hunter_mod_only_uses_hunters_exalt(self):
        sig = self.fam("hunter", "#% increased maximum Life")
        result = self.planner.plan(self.base.id, 86, [], [{"signature": sig, "influence": "hunter"}])
        best = result["strategies"][0]
        self.assertIn("Hunter's Exalted Orb", best["name"])
        step = next(s for s in best["steps"] if s["title"] == "Hunter's Exalted Orb")
        pool = self.inf.exalt_pool(self.ctx["hunter"])          # odds = share of the Hunter pool's spawn weight
        share = sum(e.weight for e in pool if e.mod.signature == sig) / sum(e.weight for e in pool)
        self.assertAlmostEqual(step["probability"], share, places=12)
        price = self.planner.prices.get("Hunter's Exalted Orb")
        self.assertAlmostEqual(best["cost_breakdown"]["influence"], price / share)
        self.assertEqual(best["status"], "approximate")
        self.assertIn("both affix sides are open", step["detail"])
        # Orb of Dominance is offered only for an Elevated target (range plan) - not here, and no longer listed
        # as "not supported"
        self.assertFalse(any("Dominance" in n["name"] for n in result["not_supported"]))
        self.assertFalse(any("Dominance" in r["name"] for r in result["comparison"]))   # never a candidate

    def test_awakener_transfer_includes_donors_and_reroll(self):
        specs = [{"signature": self.fam("hunter", "#% increased maximum Life"), "influence": "hunter"},
                 {"signature": self.fam("warlord", "+#% to all Elemental Resistances"), "influence": "warlord"},
                 {"signature": self.xfams["+#% to Cold Resistance"], "min": 30}]
        result = self.planner.plan(self.base.id, 86, [], specs)
        best = result["strategies"][0]
        self.assertEqual(best["family"], "awakener")
        self.assertEqual(best["status"], "approximate")
        titles = [s["title"] for s in best["steps"]]
        self.assertIn("Donor: Hunter item", titles)
        self.assertIn("Second item: Warlord item", titles)
        donor = next(s for s in best["steps"] if s["title"] == "Donor: Hunter item")
        self.assertGreater(donor["cost"], 0)
        self.assertIn("Buying one instead is not priced", donor["detail"])
        awk = next(s for s in best["steps"] if s["title"] == "Awakener's Orb")
        self.assertIn("not in the data", awk["detail"])                   # the kept-mod rule is flagged
        self.assertLess(awk["probability"], 1.0)                          # cold res must come from the reforge
        self.assertIsNotNone(best["sim"])                                 # and that reforge was simulated

    def test_influence_infeasible_reasons(self):
        from poecraft.engine.planner import Infeasible
        ring = self.data.base_by_name("Two-Stone Ring")
        sig = self.fam("hunter", "#% increased effect of Offerings")
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(ring.id, 86, [], [{"signature": sig, "influence": "hunter"}])
        self.assertIn("Ring has no Hunter modifier with this stat line", str(cm.exception))
        from poecraft.data.eldritch import get_eldritch
        from poecraft.engine import eldritch
        ekey = next(f.key for f in eldritch.all_families(get_eldritch()).values()
                    if f.side == "exarch" and f.label == "+#% to maximum Cold Resistance")
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(self.base.id, 86, [], [{"signature": self.fam("hunter", "#% increased maximum Life"),
                                                       "influence": "hunter"},
                                                      {"eldritch": "exarch", "signature": ekey, "min": 2}])
        self.assertIn("cannot be combined with an Eldritch implicit target", str(cm.exception))

    def test_influence_requirement_reaches_cli(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from poecraft.__main__ import main
        from poecraft.engine.planner import Infeasible
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf), self.assertRaises(Infeasible) as cm:
            main(["plan", "--base", "Vaal Regalia", "--target", "#% increased maximum Life", "--sims", "0",
                  "--no-live-prices"])
        self.assertIn("requires Elder or Hunter influence", str(cm.exception))
        self.assertIn("mark the target '(Elder)'", str(cm.exception))


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class Fracture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules = get_data(), get_rules()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.fams = {f.label: f.signature for f in ItemContext(cls.data, cls.rules, cls.base, 86, []).families().values()}
        cls.planner = Planner(prices=Prices(use_live=False), sims=1000)

    def specs(self):
        return [{"signature": self.fams["+# to maximum Life"], "min": 140, "fractured": True},
                {"signature": self.fams["+#% to Cold Resistance"], "min": 36},
                {"signature": self.fams["+# to maximum Energy Shield"], "min": 50}]

    def test_fractured_life_then_cold_and_bench(self):
        result = self.planner.plan(self.base.id, 86, [], self.specs())
        best = result["strategies"][0]
        titles = [s["title"] for s in best["steps"]]
        # Life is fractured first, then the rest (Cold Resistance roll, Energy Shield bench craft)
        self.assertLess(titles.index("Fracturing Orb"), next(i for i, t in enumerate(titles) if t.startswith("Reroll")))
        self.assertIn("Crafting bench", titles)
        orb = next(s for s in best["steps"] if s["title"] == "Fracturing Orb")
        n = {int(k): v for k, v in self.rules["rare_mod_count"].items()}
        uniform = sum(p / k for k, p in n.items())          # E[1/k] over 4/5/6-mod rares, the target always present
        self.assertAlmostEqual(orb["probability"], uniform / sum(n.values()), places=9)
        self.assertEqual(orb["status"], "approximate")
        self.assertIn("how that modifier is chosen is not in the game data", orb["detail"])
        self.assertGreater(best["cost_breakdown"]["fracture"], self.planner.prices.get("Fracturing Orb"))
        self.assertTrue(result["targets"][0]["description"].endswith("(fractured)"))
        self.assertIsNotNone(best["sim"])                    # the part after fracturing was simulated
        # Screaming essences reforge a rare (their currency text says so), so after fracturing they are offered;
        # only Whispering-Wailing would need a Normal item. (This used to assert the "lower-tier essences are
        # not offered" note, back when the rule wrongly put Screaming on the Normal-only side.)
        names = [str(c["name"]) for c in result["comparison"]]
        self.assertTrue(any("Fracturing Orb" in n and "Screaming Essence" in n for n in names))
        self.assertNotIn("Lower-tier essences are not offered after fracturing: they require a Normal item.",
                         best["notes"])

    def test_fractured_mod_is_protected_from_annul_and_scour(self):
        from poecraft.engine.analytic import RESTART, GroupedPool
        from poecraft.engine.context import ItemContext
        ctx = ItemContext(self.data, self.rules, self.base, 86, [])
        life = self.data.mods["IncreasedLife12"]
        gp = GroupedPool(ctx.pool(), ctx.make_targets([{"signature": self.fams["+#% to Cold Resistance"]}]),
                         (life,), protected=(life,))
        st = (0, gp.hits0, 1, 1)                              # life (fractured) + one other prefix + one suffix
        n, outs = gp.annul(st)
        self.assertEqual(n, 2)                                # the fractured Life mod is never a candidate
        self.assertNotIn(RESTART, [s for _, s in outs])
        self.assertNotEqual(gp.scour_side(st, "suffix"), RESTART)   # a locked Scour of the prefixes keeps it

    def test_fractured_target_on_influenced_base_is_infeasible(self):
        from poecraft.engine.planner import Infeasible
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(self.base.id, 86, ["shaper"], self.specs()[:2])
        self.assertIn("'Cannot be used on Influenced or Fractured items' (currency text)", str(cm.exception))


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class Veiled(unittest.TestCase):
    LIFE_MANA = "prefix|maximum_life_+%,maximum_mana_+%"     # unveiled '(9-10)% increased maximum Life / Mana'

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules = get_data(), get_rules()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.fams = {f.label: f.signature for f in ItemContext(cls.data, cls.rules, cls.base, 86, []).families().values()}
        cls.planner = Planner(prices=Prices(use_live=False), sims=1000)
        cls.result = cls.planner.plan(cls.base.id, 86, [], [
            {"signature": cls.fams["+# to maximum Life"], "min": 140},
            {"signature": cls.fams["+#% to Cold Resistance"], "min": 36},
            {"signature": cls.LIFE_MANA, "veiled": True}])

    def three_option_chance(self):
        """Independent check: enumerate every ordered draw of 3 options from this base's 6 veiled prefixes."""
        import itertools
        import json
        mods = json.loads((config.RAW_DIR / "mods.json").read_text(encoding="utf-8"))
        tags = set(self.base.tags)
        pool = []
        for mid, m in mods.items():
            if m["domain"] != "unveiled" or m["generation_type"] != "prefix" or m["name"] != "Chosen":
                continue
            w = next((s["weight"] for s in m["spawn_weights"] if s["tag"] in tags), 0)
            if w > 0:
                pool.append((mid, w))
        target = "JunMasterVeiledPercentageLifeAndMana"
        total = sum(w for _, w in pool)
        p_hit = 0.0
        for seq in itertools.permutations(pool, 3):
            p, left = 1.0, total
            for _, w in seq:
                p *= w / left
                left -= w
            p_hit += p if any(mid == target for mid, _ in seq) else 0.0
        return p_hit

    def test_aisling_into_veiled_target(self):
        best = self.result["strategies"][0]
        self.assertTrue(best["family"].endswith("+aisling"), best["family"])
        titles = [s["title"] for s in best["steps"]]
        reroll = next(i for i, t in enumerate(titles) if t.startswith("Reroll"))
        ais, unv = titles.index("Aisling (Research, rank 3)"), titles.index("Unveil (Jun)")
        self.assertLess(reroll, ais)                         # explicit targets first ...
        self.assertEqual(unv, ais + 1)                       # ... then Aisling, then the unveil
        self.assertIn("free prefix for Aisling's veiled modifier", best["steps"][reroll]["expected_state"])
        u = best["steps"][unv]
        self.assertAlmostEqual(u["probability"], self.three_option_chance(), places=9)
        self.assertEqual(u["status"], "approximate")
        self.assertIn("not how the 3 unveil options are drawn", u["detail"])
        fails = " ".join(b["text"] for b in u["on_fail"])
        self.assertIn("Start over on a fresh base", fails)                 # both recoveries modelled ...
        self.assertIn("Orb of Annulment until it removes the wrong unveiled modifier", fails)
        self.assertIn("this is what the cost assumes", fails)              # ... and one chosen
        self.assertEqual(best["steps"][ais]["status"], "approximate")
        self.assertIn("not in the game data at all", best["steps"][ais]["detail"])
        note = ("Aisling model assumes no existing mod is removed. If the real Research craft removes a random mod, "
                "the cost here is understated and the success chance overstated.")
        self.assertIn(note, best["notes"])                   # once in the plan notes ...
        self.assertEqual([s for s in best["steps"] if note in (s.get("detail") or "")],
                         [best["steps"][ais]])                # ... and on the Aisling step only
        self.assertGreater(best["cost_breakdown"]["veiled"], 0)
        self.assertTrue(any(t.get("veiled") for t in self.result["targets"]))

    def test_veiled_chaos_into_veiled_target(self):
        vc = next(s for s in self.result["strategies"] if s["family"] == "veiled_chaos")
        titles = [s["title"] for s in vc["steps"]]
        self.assertIn("Reroll: Veiled Chaos Orb", titles)
        u = vc["steps"][titles.index("Unveil (Jun)")]
        self.assertEqual(u["status"], "approximate")
        self.assertIn("50% assumed (not in the data)", u["detail"])
        p_u = self.three_option_chance()
        self.assertAlmostEqual(u["probability"], p_u, places=9)
        # odds per Veiled Chaos = (other targets roll, one slot held by the veiled prefix) x 50% side x unveil hit
        p_roll = vc["steps"][titles.index("Reroll: Veiled Chaos Orb")]["probability"]
        self.assertAlmostEqual(vc["p_attempt"], p_roll * 0.5 * p_u, places=12)
        self.assertAlmostEqual(vc["cost_breakdown"]["rng"],
                               self.planner.prices.get("Veiled Chaos Orb") / vc["p_attempt"], places=6)
        self.assertIsNotNone(vc["sim"])                      # the reroll part was simulated

    def test_veiled_target_on_base_that_cannot_get_it_is_infeasible(self):
        from poecraft.data.veiled import get_veiled
        from poecraft.engine.planner import Infeasible
        weapon = next(u.mod for u in get_veiled().mods        # weapon-only unveiled outcome
                      if u.mod.id == "JunMasterVeiledLocalIncreasedPhysicalDamageAndImpaleCrafted")
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(self.base.id, 86, [], [{"signature": weapon.signature, "veiled": True}])
        rep = cm.exception.report
        self.assertEqual(len(rep["unreachable"]), 1)
        self.assertIn("not in the unveil pool of Body Armour", rep["unreachable"][0]["reason"])


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class RangePlans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules = get_data(), get_rules()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.planner = Planner(prices=Prices(use_live=False), sims=1000)

    def note(self, mech):
        return (f"Odds for {mech} are not in the game data. This plan shows a range, not a single number; treat it "
                f"as a schematic, not a quote.")

    def test_elevated_target_via_orb_of_dominance_is_a_range(self):
        from poecraft.data.elevated import get_elevated
        edata = get_elevated()
        target = next((e for e in edata.mods if e.mod.id == "MaximumLifeUberMaven"), None)
        spec = {"signature": "prefix|maximum_life_+%", "elevated": "elder"}
        if target is None or target.base_type is None:          # no Elevated pool / link in this data export
            from poecraft.engine.planner import Infeasible
            with self.assertRaises(Infeasible) as cm:
                self.planner.plan(self.base.id, 86, ["elder"], [spec])
            self.assertIn("not supported (missing outcome data)", str(cm.exception))
            return
        result = self.planner.plan(self.base.id, 86, ["elder"], [
            spec, {"signature": "suffix|base_cold_damage_resistance_%", "min": 36}])
        best = result["strategies"][0]
        self.assertTrue(result["schematic"])                     # nothing deterministic reaches an Elevated mod
        self.assertIsNone(best["expected_cost"])                 # never a single number
        cr = best["cost_range"]
        self.assertIsNone(cr["high"])
        self.assertLess(cr["low"], cr["assumed"])
        self.assertIn("best case: the Orb of Dominance always upgrades the target's base modifier (100%)",
                      cr["low_reason"])
        self.assertIn("worst case: the target's base modifier is always the one removed (0%)", cr["high_reason"])
        self.assertIn("an assumption, not in the game data", cr["assumed_reason"])
        self.assertIn("no upper bound", cr["text"])
        self.assertIn(self.note("Orb of Dominance"), best["notes"])
        orb = next(s for s in best["steps"] if s["title"] == "Orb of Dominance")
        self.assertNotIn("probability", orb)
        self.assertIn("best case 100%", orb["probability_text"])
        self.assertIn("worst case 0%", orb["probability_text"])
        self.assertEqual(result["comparison"][0]["cost_range"]["text"], cr["text"])
        self.assertTrue(result["comparison"][0]["why"].startswith("SCHEMATIC ONLY"))

    def test_t1_eldritch_target_via_orb_of_conflict_is_a_range(self):
        from poecraft.data.eldritch import get_eldritch
        from poecraft.engine import eldritch
        fams = eldritch.base_families(get_eldritch(), self.base.item_class, set(self.base.tags))
        f = next(f for f in fams.values() if f.side == "exarch" and {1, 2, 3} <= set(f.tiers)
                 and f.tiers[1].stats[0].max > f.tiers[2].stats[0].max)
        result = self.planner.plan(self.base.id, 86, [], [
            {"eldritch": "exarch", "signature": f.key, "min": f.tiers[2].stats[0].max + 1}])   # only tier 1 reaches it
        best = result["strategies"][0]
        self.assertTrue(result["schematic"])
        cr = best["cost_range"]
        self.assertIsNone(cr["high"])
        self.assertIsNone(cr["assumed"])                         # no defensible middle estimate
        self.assertIn("no defensible middle estimate", cr["assumed_reason"])
        self.assertIn("2 needed for tier 1", cr["low_reason"])
        self.assertIn(self.note("Orb of Conflict"), " ".join(s.get("detail") or "" for s in best["steps"]))
        orb = next(s for s in best["steps"] if s["title"].startswith("Orb of Conflict"))
        self.assertNotIn("probability", orb)
        self.assertIn("unknown", orb["probability_text"])
        # lower bound = tier 3 step + 2 x (Lesser Ichor + Orb of Conflict)
        P = self.planner.prices.get
        t3 = next(s for s in best["steps"] if s["title"].endswith("tier 3"))
        other = next(s for s in best["steps"] if s["title"] == "Eldritch implicit (Eater of Worlds)")
        self.assertAlmostEqual(cr["low"], t3["cost"] + 2 * (other["cost"] + P("Orb of Conflict")), places=6)

    def test_range_plan_cannot_win_when_worst_case_exceeds_best_deterministic(self):
        from poecraft.engine.planner import OBJECTIVES, Strategy
        m = lambda c: {"cost": c, "sd": 0.0, "risk": c, "apps": 1.0, "var_rng": 0.0, "var_div": 0.0, "apps_rng": 1.0}
        det = Strategy("chaos", "Chaos Orb spam", None, [], 0.1, 100.0, 0, 0, 0, params={"per": 10.0})
        inner = Strategy("chaos", "Chaos Orb spam", None, [], 0.5, 20.0, 0, 0, 0, params={"per": 10.0})
        rng = Strategy("chaos+dominance", "Chaos Orb spam", None, [], 0.5, 20.0, 0, 0, 0, cost_dominance=30.0,
                       params={"per": 10.0, "inner": inner, "dominance": {"p_uniform": 0.5, "n": 2, "price": 30.0}})
        det.params["metrics"], rng.params["metrics"] = m(det.cost), m(rng.cost)
        self.assertLess(rng.cost, det.cost)                      # cheaper in the best case ...
        obj = OBJECTIVES["min_cost"]
        best, demoted, verified, schematic = self.planner._select([rng, det], lambda s: (True, True, ""), obj)
        self.assertIs(best, det)                                 # ... but its worst case has no upper bound
        self.assertFalse(schematic)
        rows = self.planner._comparison([det, rng], best, demoted, obj)
        row = next(r for r in rows if r["cost_range"])
        self.assertEqual(row["why"], "demoted: range-based, worst case exceeds the best deterministic plan "
                                     "(no upper bound - odds unknown)")
        self.assertNotEqual(row["why"], "RECOMMENDED")


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class SpecialMechanics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.data.special import get_special
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules, cls.special = get_data(), get_rules(), get_special()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        cls.planner = Planner(prices=Prices(use_live=False), sims=1000)

    def test_beastcraft_target_follows_the_real_data_path(self):
        from poecraft.engine.planner import Infeasible
        aspect = next(m for m in self.special.beast_outcomes if "Aspect of the Cat" in m.text)
        spec = {"signature": aspect.signature, "beastcraft": True}
        if self.special.recipes:             # the export gained recipes: the plan must contain a Beastcraft step
            best = self.planner.plan(self.base.id, 86, [], [spec])["strategies"][0]
            self.assertTrue(any("Beastcraft" in s["title"] for s in best["steps"]))
            return
        with self.assertRaises(Infeasible) as cm:   # today: no recipe file, so the missing piece is named
            self.planner.plan(self.base.id, 86, [], [spec])
        reason = cm.exception.report["unreachable"][0]["reason"]
        self.assertIn("not supported (missing recipe data)", reason.lower())
        self.assertIn("no Beastcrafting recipes", reason)

    def test_special_fossil_target_follows_the_real_data_path(self):
        from poecraft.engine import special
        hollow = self.special.fossils["Hollow Fossil"]
        socket = next(m for m in hollow.forced if "Abyssal Socket" in m.text)
        specs = [{"signature": socket.signature, "fossil": "Hollow Fossil"},
                 {"signature": "prefix|base_maximum_life", "min": 100}]
        status, reason, assumptions = special.verdict(hollow, self.rules)
        if status != special.SUPPORTED:      # data missing: the reason names the piece
            from poecraft.engine.planner import Infeasible
            with self.assertRaises(Infeasible) as cm:
                self.planner.plan(self.base.id, 86, [], specs)
            self.assertIn("not supported (missing fossil mod data)", str(cm.exception))
            return
        result = self.planner.plan(self.base.id, 86, [], specs)
        best = result["strategies"][0]
        self.assertTrue(best["name"].startswith("Fossil spam: Hollow"))
        reroll = next(s for s in best["steps"] if s["title"].startswith("Reroll: Hollow Fossil"))
        self.assertEqual(best["status"], "approximate")
        self.assertTrue(any(n.startswith("Special fossil: Hollow Fossil (game data: Has an Abyssal socket)")
                            and "assumed to count toward the usual 4-6 mod count" in n for n in best["notes"]))
        # odds: the forced socket is always there (it holds one suffix), so they are the Life odds of that pool
        from poecraft.engine.analytic import GroupedPool
        from poecraft.engine.context import ItemContext, Target
        ctx = ItemContext(self.data, self.rules, self.base, 86, [])
        names = [n.strip() + " Fossil" for n in best["name"].split(": ", 1)[1].split("+")]
        combo = tuple(next(f for f in self.data.fossils if f.name == n) for n in names)
        pool = special.pool(ctx, combo, hollow, self.rules["fossil_weight_divisor"])
        life = Target(0, "prefix|base_maximum_life", "base_maximum_life", 100, "+# to maximum Life", "prefix")
        gp = GroupedPool(pool, [life], (socket,))
        rare_n = {int(k): v for k, v in self.rules["rare_mod_count"].items()}
        total = sum(rare_n.values())
        p = gp.success(gp.roll({k: v / total for k, v in rare_n.items()}, ctx.limits), 1, None, ctx.limits)
        self.assertAlmostEqual(reroll["probability"], p, places=9)
        self.assertTrue(any(t.get("special_fossil") == "Hollow Fossil" for t in result["targets"]))
        self.assertIsNotNone(best["sim"])                    # the reroll was simulated
        self.assertFalse(best["sim"].get("disagrees"))

    def test_beastcraft_only_target_reason_reaches_cli_and_ui(self):
        import io
        from contextlib import redirect_stdout
        from poecraft.__main__ import _targets_from_cli
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Infeasible
        from poecraft.server import _families
        ctx = ItemContext(self.data, self.rules, self.base, 86, [])
        with redirect_stdout(io.StringIO()):                 # CLI: an unmarked target that only a beast gives
            specs = _targets_from_cli(ctx, ["Aspect of the Cat"])
        self.assertTrue(specs[0].get("beastcraft"))
        with self.assertRaises(Infeasible) as cm:
            self.planner.plan(self.base.id, 86, [], specs)
        self.assertIn("not supported (missing recipe data)", str(cm.exception).lower())
        # UI: the mod list shows the outcome with the reason, and features name Beastcrafting on its own line
        fams = _families({"base": [self.base.id], "ilvl": ["86"]})["families"]
        cat = next(f for f in fams if f.get("beastcraft") and "Aspect of the Cat" in f["label"])
        self.assertIn("no Beastcrafting recipes", cat["not_supported"])
        rows = [f for f in self.planner.plan(self.base.id, 86, [], [
            {"signature": "prefix|base_maximum_life", "min": 100}])["features"] if f["name"] == "Beastcrafting"]
        self.assertEqual(rows[0]["status"], "not supported (missing recipe data)")


class Localization(unittest.TestCase):
    def test_switching_language_changes_ui_strings(self):
        from poecraft import i18n
        en = i18n.strings("en")
        codes = [l["code"] for l in i18n.languages()]
        self.assertEqual(codes[0], "en")
        self.assertTrue({"ru", "de"} <= set(codes))
        for code in ("ru", "de"):
            self.assertEqual(set(i18n.strings(code)), set(en))           # complete: nothing falls back
            for key in ("run.build", "plan.recommended", "table.col.why", "prices.update", "cli.expected"):
                text = i18n.t(code, key)
                self.assertNotEqual(text, i18n.t("en", key))
                self.assertFalse(text.startswith(i18n.FALLBACK_MARK))
        self.assertEqual(i18n.t("de", "plan.recommended"), "EMPFOHLEN")
        self.assertEqual(i18n.t("ru", "plan.recommended"), "РЕКОМЕНДУЕТСЯ")

    def test_missing_ui_string_falls_back_to_english_with_marker(self):
        import json
        import tempfile
        from pathlib import Path
        from poecraft import i18n
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "en.json").write_text(json.dumps({"_meta": {"name": "English"}, "a": "Apple {n}", "b": "Bean"}),
                                          encoding="utf-8")
            (root / "xx.json").write_text(json.dumps({"_meta": {"name": "Test"}, "a": "Apfel {n}"}), encoding="utf-8")
            self.assertEqual(i18n.t("xx", "a", root, n=2), "Apfel 2")
            self.assertEqual(i18n.t("xx", "b", root), i18n.FALLBACK_MARK + "Bean")     # English, marked
            self.assertEqual(i18n.t("en", "b", root), "Bean")                          # no marker in English
            self.assertEqual([l["code"] for l in i18n.languages(root)], ["en", "xx"])  # new file = new language
            i18n._load.cache_clear()

    @unittest.skipUnless(config.DB_PATH.exists() and (config.DATA_DIR / "locale_game_ru.json").exists(),
                         "database or game-data locale files not built - run `python -m poecraft update`")
    def test_game_data_is_localized_by_internal_id(self):
        # replaces test_game_data_strings_stay_english_in_every_language: game data is now localized by id
        import io
        from contextlib import redirect_stdout
        from poecraft import __main__ as cli, i18n
        from poecraft.server import _families
        self.assertEqual(i18n.game_text("mod", "IncreasedLife12", "+(175-189) to maximum Life", "en"),
                         ("+(175-189) to maximum Life", True))                       # English needs no file
        self.assertEqual(i18n.game_text("mod", "IncreasedLife12", "x", "ru"), ("+(175-189) к максимуму здоровья", True))
        self.assertEqual(i18n.game_text("base", "Metadata/Items/Currency/CurrencyRerollRare", "Chaos Orb", "de"),
                         ("Chaossphäre", True))
        base = "Metadata/Items/Armours/BodyArmours/BodyInt17"
        fams = {lang: _families({"base": [base], "ilvl": ["86"], "lang": [lang]})["families"] for lang in ("en", "ru")}
        self.assertEqual([f["key"] for f in fams["en"]], [f["key"] for f in fams["ru"]])      # same mods, same order
        life = next(i for i, f in enumerate(fams["en"]) if f["label"] == "+# to maximum Life")
        self.assertEqual(fams["ru"][life]["label"], "+# к максимуму здоровья")                # number-to-# step
        self.assertNotIn("label", fams["ru"][life].get("_en", []))
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                cli.main(["plan", "--base", "Vaal Regalia", "--target", "+# to maximum Life>=140", "--sims", "0",
                          "--no-live-prices", "--lang", "de"])
        finally:
            cli.LANG = "en"
        text = out.getvalue()
        self.assertIn("EMPFOHLEN", text)
        self.assertIn("=== Vaal-Gewand ([EN] Body Armour), Stufe 86 ===", text)   # item class: no locale data -> [EN]
        self.assertIn("+# zu maximalem Leben (>= 140)", text)
        # explanations are translated now (this assertion used to check the old "[EN] Plan-Erläuterungen ..." banner)
        self.assertNotIn("Plan-Erläuterungen", text)
        self.assertIn("Neuwurf: Kreischende Essenz der Gier", text)                 # step title + localized essence

    @unittest.skipUnless(config.DB_PATH.exists() and (config.DATA_DIR / "locale_game_ru.json").exists(),
                         "database or game-data locale files not built - run `python -m poecraft update`")
    def test_russian_plan_has_the_same_ids_only_display_differs(self):
        from poecraft import i18n
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        base = get_data().base_by_name("Vaal Regalia")
        specs = [{"signature": "prefix|base_maximum_life", "min": 140},
                 {"signature": "suffix|base_cold_damage_resistance_%", "min": 36}]
        result = Planner(prices=Prices(use_live=False), sims=0).plan(base.id, 86, [], specs)
        en, ru = i18n.localize_plan(result, "en"), i18n.localize_plan(result, "ru")
        self.assertEqual(en["item"]["base_id"], ru["item"]["base_id"])
        self.assertEqual([t["game_id"] for t in en["targets"]], [t["game_id"] for t in ru["targets"]])
        self.assertEqual(en["strategies"], ru["strategies"])                  # the plan itself is untouched
        self.assertEqual(en["comparison"], ru["comparison"])
        self.assertNotEqual(en["item"]["base"], ru["item"]["base"])           # only display text differs
        self.assertEqual(ru["item"]["base"], "Наряд ваал")
        self.assertEqual([t["description"] for t in ru["targets"]],
                         ["+# к максимуму здоровья (>= 140)", "+#% к сопротивлению холоду (>= 36)"])
        self.assertEqual([t.get("_en") for t in ru["targets"]], [None, None])
        self.assertEqual(ru["item"]["_en"], ["item_class"])

    @unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
    def test_missing_localized_string_falls_back_to_english_with_en_mark(self):
        import json
        import tempfile
        from pathlib import Path
        from poecraft import __main__ as cli, i18n
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        base = get_data().base_by_name("Vaal Regalia")
        result = Planner(prices=Prices(use_live=False), sims=0).plan(
            base.id, 86, [], [{"signature": "prefix|base_maximum_life", "min": 140}])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)                                   # a language file that only knows the base name
            (root / "locale_game_xx.json").write_text(json.dumps({"_meta": {"language": "Test"}, base.id: "Basis"}),
                                                      encoding="utf-8")
            self.assertEqual(i18n.game_text("mod", "IncreasedLife12", "+(175-189) to maximum Life", "xx", root),
                             ("+(175-189) to maximum Life", False))
            out = i18n.localize_plan(result, "xx", root)
            self.assertEqual(out["item"]["base"], "Basis")
            t = out["targets"][0]
            self.assertEqual(t["description"], "+# to maximum Life (>= 140)")   # English, never translated ...
            self.assertEqual(t["_en"], ["description"])                          # ... and marked
            self.assertEqual(cli._g(t, "description"), "[EN] +# to maximum Life (>= 140)")
            self.assertEqual(cli._g(out["item"], "base"), "Basis")
            self.assertFalse(i18n.game_data_status("yy", root)["available"])     # no file at all
            i18n._game_map.cache_clear()

    def test_locale_update_refuses_a_different_build_and_drops_incomplete_texts(self):
        import json
        import tempfile
        from pathlib import Path
        from poecraft.data import locales
        en_b = {"Metadata/A": {"name": "Chaos Orb", "item_class": "StackableCurrency", "properties": {"description": "x"}}}
        en_m = {"M1": {"text": "+1 to Life", "name": "Hale", "domain": "item", "stats": [1]},
                "M2": {"text": "line one\nline two", "name": "", "domain": "item", "stats": [2]}}
        ru_b = {"Metadata/A": {"name": "Сфера хаоса", "item_class": "StackableCurrency", "properties": {"description": "у"}}}
        ru_m = {"M1": {"text": "+1 к здоровью", "name": "Здоровый", "domain": "item", "stats": [1]},
                "M2": {"text": "только одна строка", "name": "", "domain": "item", "stats": [2]}}

        def run(bases, mods):
            d = Path(tempfile.mkdtemp())
            (d / "Russian").mkdir()
            for name, data in (("base_items", en_b), ("mods", en_m)):
                (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
            (d / "Russian" / "base_items.json").write_text(json.dumps(bases), encoding="utf-8")
            (d / "Russian" / "mods.json").write_text(json.dumps(mods), encoding="utf-8")
            return d

        d = run(ru_b, ru_m)
        meta = locales.build("ru", "Russian", d, d)
        built = json.loads((d / "locale_game_ru.json").read_text(encoding="utf-8"))
        self.assertEqual(built["Metadata/A"], "Сфера хаоса")
        self.assertEqual(built["M1"], "+1 к здоровью")
        self.assertNotIn("M2", built)                                   # a stat line is missing: stays English
        self.assertEqual(meta["excluded_incomplete_mod_texts"], ["M2"])
        for bad_b, bad_m, why in (
                (ru_b, {"M1": ru_m["M1"]}, "ids missing from the localized export"),
                (ru_b, dict(ru_m, M1=dict(ru_m["M1"], stats=[9])), "non-localized fields differ"),
                ({"Metadata/A": dict(ru_b["Metadata/A"], item_class="Other")}, ru_m, "non-localized fields differ")):
            d = run(bad_b, bad_m)
            with self.assertRaises(locales.LocaleMismatch) as cm:
                locales.build("ru", "Russian", d, d)
            self.assertIn(why, str(cm.exception))
            self.assertFalse((d / "locale_game_ru.json").exists())      # no partial mapping written


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class Explanations(unittest.TestCase):
    """Plan explanations are message keys rendered at the edge: one computed plan, any language."""

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        data = get_data()
        cls.base = data.base_by_name("Vaal Regalia")
        cls.raw = Planner(prices=Prices(use_live=False), sims=500).plan(cls.base.id, 86, [], [
            {"signature": "prefix|base_maximum_life", "min": 140},
            {"signature": "prefix|local_energy_shield", "min": 50},
            {"signature": "suffix|base_cold_damage_resistance_%", "min": 36}], lang=None)

    def test_switching_language_changes_explanation_text(self):
        import re
        from poecraft.messages import missing_keys, render
        out = {lang: render(self.raw, lang) for lang in ("en", "ru", "de")}
        steps = {lang: out[lang]["strategies"][0]["steps"] for lang in out}
        self.assertEqual([s["title"] for s in steps["en"]][0], "Get the base")
        self.assertEqual([s["title"] for s in steps["ru"]][0], "Возьмите базу")
        self.assertEqual([s["title"] for s in steps["de"]][0], "Basis besorgen")
        for lang in ("ru", "de"):
            self.assertEqual(missing_keys(self.raw, lang), [])                 # every key used here is translated
            self.assertNotEqual(out[lang]["selection_note"], out["en"]["selection_note"])
            self.assertNotEqual(out[lang]["strategies"][0]["notes"], out["en"]["strategies"][0]["notes"])
            self.assertNotEqual(out[lang]["strategies"][0]["name"], out["en"]["strategies"][0]["name"])
            self.assertNotEqual(out[lang]["comparison"][1]["why"], out["en"]["comparison"][1]["why"])
            # the only EN marks left are game names with no locale data (here: the bench master's name)
            marked = re.findall(r"\[EN\] ([^)(;,.]+)", " ".join(s["action"] for s in steps[lang]))
            self.assertTrue(all(m.startswith("Niko") for m in marked), marked)
        # a game name inside a sentence is a parameter: the Russian step names the essence the way the game does
        self.assertIn("сущность", steps["ru"][1]["title"].lower())

    def test_missing_key_falls_back_to_english_with_en_mark(self):
        from unittest import mock
        from poecraft import i18n
        from poecraft.messages import M, missing_keys, render
        real = i18n._load

        def partial(code, root=i18n.LOCALES_DIR):            # a language that has only one of the keys below
            return {"m.base.title": "Basis (xx)"} if code == "xx" else real(code, root)

        with mock.patch.object(i18n, "_load", partial):
            self.assertEqual(M("m.base.title").render("xx"), "Basis (xx)")
            self.assertEqual(M("m.done.title").render("xx"), "[EN] Done")      # English, marked - never translated
            steps = render(self.raw, "xx")["strategies"][0]["steps"]
            self.assertEqual(steps[0]["title"], "Basis (xx)")
            self.assertEqual(steps[-1]["title"], "[EN] Done")
            missing = missing_keys(self.raw, "xx")
            self.assertIn("m.done.title", missing)
            self.assertNotIn("m.base.title", missing)

    def test_ids_and_numbers_identical_across_languages(self):
        from poecraft.messages import render
        out = {lang: render(self.raw, lang) for lang in ("en", "ru", "de")}

        def leaves(obj, path=""):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    yield from leaves(v, f"{path}.{k}")
            elif isinstance(obj, (list, tuple)):
                for i, v in enumerate(obj):
                    yield from leaves(v, f"{path}[{i}]")
            else:
                yield path, obj

        en = dict(leaves(out["en"]))
        for lang in ("ru", "de"):
            other = dict(leaves(out[lang]))
            self.assertEqual(set(en), set(other))                          # same structure, same number of rows
            for path, v in en.items():
                if isinstance(v, (int, float, bool)) or v is None:
                    self.assertEqual(other[path], v, path)                 # every number / flag identical
                elif path.endswith(("base_id", "game_id", ".family", ".status", ".key", "mod_id")):
                    self.assertEqual(other[path], v, path)                 # every internal id identical

    def test_english_templates_match_the_rules_and_every_key_is_translated(self):
        import json
        import re
        from pathlib import Path
        from poecraft import i18n
        from poecraft.data.repository import get_rules
        from poecraft.engine.planner import feature_rows, rule_note
        rules = get_rules()
        for name, text in rules.items():                        # rules notes: English rendering == mechanics.json
            if isinstance(text, str) and (name.endswith("_note") or name == "beastcraft_missing") \
                    and i18n.lookup("en", f"m.rule.{name}")[1] is False:
                self.assertEqual(str(rule_note(name)), text, name)
        for f, row in zip(rules["features"], feature_rows(rules)):
            self.assertEqual(str(row["name"]), f["name"])
            if f.get("note"):
                self.assertEqual(str(row["note"]), f["note"])
        # every literal message key in the code exists in en, ru and de
        src = "".join(p.read_text(encoding="utf-8") for p in Path("poecraft").rglob("*.py"))
        used = set(re.findall(r'M\(\s*"(m\.[\w.]+)"', src))
        used |= {f"m.rule.{n}" for n, t in rules.items() if isinstance(t, str) and n.endswith("_note")}
        used |= {f"m.fx.{k}" for k in rules["fossil_effects"]} | {f"m.harvest.{k}" for k in rules["harvest_reforge_tags"]}
        used |= {f"m.feature.{f['id']}.name" for f in rules["features"]}
        for lang in ("en", "ru", "de"):
            have = json.loads((i18n.LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(k for k in used if k not in have), [], lang)


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class TimeLimitsAndProgress(unittest.TestCase):
    """Wall-clock limits cut only the simulation check (and the optional recovery variant); progress is reported."""
    TARGETS = [{"signature": "prefix|base_maximum_life", "min": 140},
               {"signature": "suffix|base_cold_damage_resistance_%", "min": 36},
               {"signature": "suffix|base_lightning_damage_resistance_%", "min": 36}]

    def planner(self, **kw):
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        return Planner(prices=Prices(use_live=False), **kw)

    def setUp(self):
        from poecraft.data.repository import get_data
        self.base = get_data().base_by_name("Vaal Regalia").id

    def test_default_simulation_count_is_2000(self):
        self.assertEqual(config.DEFAULT_SIMULATIONS, 2000)

    def test_simulation_timeout_is_flagged_and_fast_calculation_unchanged(self):
        full = self.planner(sims=3000).plan(self.base, 86, [], self.TARGETS, lang="en")
        cut = self.planner(sims=3000, sim_time_limit=0.0).plan(self.base, 86, [], self.TARGETS, lang="en")
        self.assertEqual(full["simulation"]["timed_out"], [])
        self.assertTrue(cut["simulation"]["timed_out"])
        self.assertTrue(any(w.startswith("Simulation timed out. Results are approximate") for w in cut["warnings"]))
        sims = [s["sim"] for s in cut["strategies"] if s["sim"]]
        parts = [p for x in sims for p in ([x] if "n" in x else [x["alt"], x["chain"]])]
        self.assertTrue(any(p.get("timed_out") and "low confidence (timeout)" in p["low_confidence"] for p in parts))
        # a timed-out part stopped short of its own target (Alteration parts may use up to 3x the runs in rolls)
        self.assertTrue(all(p["n"] < p["target"] for p in parts if p.get("timed_out")),
                        [(p["n"], p["target"]) for p in parts if p.get("timed_out")])
        # the analytic fast calculation is never cut short: same strategies, same calculated costs
        fast = lambda r: sorted((str(c["name"]), round(c["cost"], 6)) for c in r["comparison"])
        self.assertEqual(fast(cut), fast(full))

    def test_simulation_phase_limit_skips_the_rest(self):
        r = self.planner(sims=2000, sim_phase_limit=0.0).plan(self.base, 86, [], self.TARGETS, lang="en")
        self.assertTrue(r["simulation"]["skipped"])
        self.assertTrue(all(s["sim"] is None for s in r["strategies"]))
        self.assertTrue(any("were not simulated" in w for w in r["warnings"]))

    def test_recovery_time_limit_skips_only_the_recovery_variant(self):
        pl = self.planner(sims=0)
        pl.rec_time_limit = 0.0
        r = pl.plan(self.base, 86, [], self.TARGETS, lang="en")
        self.assertTrue(r["simulation"]["recovery_skipped"])
        self.assertFalse(any("Annul/metacraft recovery" in str(c["name"]) for c in r["comparison"]))
        full = self.planner(sims=0).plan(self.base, 86, [], self.TARGETS, lang="en")
        plain = lambda res: sorted((str(c["name"]), round(c["cost"], 6)) for c in res["comparison"]
                                   if "recovery" not in str(c["name"]))
        self.assertEqual(plain(r), plain(full))           # every other strategy is computed exactly as before

    def test_progress_phases_in_order(self):
        events = []
        self.planner(sims=500, progress=events.append).plan(self.base, 86, [], self.TARGETS, lang=None)
        phases = [e["phase"] for e in events]
        self.assertEqual(phases[0], "calculating")
        self.assertIn("simulating", phases)
        self.assertEqual(phases[-1], "done")
        self.assertLess(phases.index("calculating"), phases.index("simulating"))
        calc = [e for e in events if e["phase"] == "calculating"]
        self.assertTrue(all(0 <= e["done"] <= e["total"] for e in calc))
        self.assertTrue(any(e["total"] > 0 for e in calc))

    def test_cli_prints_progress_to_stderr_only(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from poecraft.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            main(["plan", "--base", "Vaal Regalia", "--target", "+# to maximum Life>=140", "--sims", "200",
                  "--no-live-prices"])
        self.assertIn("Done", err.getvalue())
        self.assertRegex(err.getvalue(), r"Calculating strategies… \d+/\d+")
        self.assertNotIn("Calculating strategies", out.getvalue())


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class SearchAndTerminology(unittest.TestCase):
    def test_no_perekaty_and_game_terms_in_russian(self):
        import json
        from poecraft import i18n
        ru = (i18n.LOCALES_DIR / "ru.json").read_text(encoding="utf-8")
        self.assertNotIn("ерекат", ru)
        self.assertNotIn("потусторон", ru)                 # the game says «Мистическая сфера …» for Eldritch
        self.assertEqual(json.loads(ru)["cost.rolling"], "Попытки")

    def test_item_class_stays_english_and_marked(self):
        from poecraft import i18n
        from poecraft.data.repository import get_data
        b = get_data().base_by_name("Velour Boots")
        name, ok = i18n.game_text("base", b.id, b.name, "ru")
        self.assertTrue(ok)                                    # the base name is the game's own Russian string
        self.assertEqual(name, "Велюровые ботинки")
        plan = i18n.localize_plan({"item": {"base": b.name, "base_id": b.id, "item_class": b.item_class}}, "ru")
        self.assertEqual(plan["item"]["item_class"], "Boots")  # no locale data for item classes: never translated
        self.assertIn("item_class", plan["item"]["_en"])

    def test_families_carry_english_label_for_search(self):
        from poecraft import server
        from poecraft.data.repository import get_data
        b = get_data().base_by_name("Crusader Boots")
        q = {"base": [b.id], "ilvl": ["86"], "lang": ["ru"]}
        fams = server._families(q)["families"]
        hybrid = [f for f in fams if f.get("label_en") == "#% increased Armour and Energy Shield"]
        self.assertTrue(hybrid)
        self.assertNotEqual(hybrid[0]["label"], hybrid[0]["label_en"])      # shown in Russian, searchable in English


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class EssenceTiersAndOptionalDivine(unittest.TestCase):
    TARGETS = [{"signature": "prefix|base_maximum_life", "min": 140},
               {"signature": "prefix|local_energy_shield", "min": 50},
               {"signature": "suffix|base_cold_damage_resistance_%", "min": 36},
               {"signature": "suffix|base_lightning_damage_resistance_%", "min": 36}]

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data, cls.rules = get_data(), get_rules()
        cls.base = cls.data.base_by_name("Vaal Regalia").id
        cls.planner = Planner(prices=Prices(use_live=False), sims=0)
        cls.en = cls.planner.plan(cls.base, 86, [], cls.TARGETS, lang="en")

    def test_rule_matches_each_essences_own_currency_text(self):
        import json
        raw = json.loads((config.RAW_DIR / "base_items.json").read_text(encoding="utf-8"))
        checked = 0
        for e in self.data.essences:
            text = (raw.get(e.id) or {}).get("properties", {}).get("description", "")
            if not text:
                continue
            reforges = "reforges a rare item" in text
            needs_normal = self.planner._needs_normal(e)
            self.assertEqual(reforges, not needs_normal, f"{e.name}: {text}")
            checked += 1
        self.assertGreater(checked, 50)
        screaming = next(e for e in self.data.essences if e.name == "Screaming Essence of Hatred")
        self.assertFalse(self.planner._needs_normal(screaming))

    def test_no_scouring_advice_for_screaming_or_shrieking(self):
        for s in self.en["strategies"]:
            for st in s["steps"]:
                if "Screaming Essence" in st["title"] or "Shrieking Essence" in st["title"]:
                    for f in st["on_fail"]:
                        self.assertNotIn("Orb of Scouring", f["text"], st["title"])
                        self.assertNotIn("fresh base", f["text"])

    def test_divine_step_is_optional_and_not_in_expected_cost(self):
        best = self.en["strategies"][0]
        div = next(st for st in best["steps"] if st.get("optional"))
        self.assertEqual(div["title"], "Divine Orb - OPTIONAL (high cost)")
        cb = best["cost_breakdown"]
        parts = sum(v for k, v in cb.items() if k != "divine")
        self.assertAlmostEqual(best["expected_cost"], parts, places=6)            # Divines excluded
        od = best["optional_divine"]
        self.assertAlmostEqual(od["cost"], cb["divine"], places=6)
        self.assertAlmostEqual(od["total_with"], best["expected_cost"] + od["cost"], places=6)
        self.assertTrue(0 < od["p_values_met"] <= 1)
        self.assertLess(best["metrics"]["bad_luck_cost"], best["metrics"]["with_optional_divine"]["bad_luck_cost"])
        from poecraft.messages import render
        ru = render(self.planner.plan(self.base, 86, [], self.TARGETS, lang=None), "ru")
        ru_div = next(st for st in ru["strategies"][0]["steps"] if st.get("optional"))
        self.assertEqual(ru_div["title"], "«Божественная сфера» — ПО ЖЕЛАНИЮ (дорого)")

    def test_ranking_still_meets_the_thresholds(self):
        # Screaming Essence of Greed is cheaper but meets Life >= 140 on a third of items; Shrieking always does.
        r = self.planner.plan(self.base, 86, [], [{"signature": "prefix|base_maximum_life", "min": 140}], lang="en")
        self.assertEqual(str(r["recommended"]), "Essence spam: Shrieking Essence of Greed")


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class GuaranteedEssenceMod(unittest.TestCase):
    """Shrieking Essence of Spite guarantees '+(43-50) to Intelligence' on armour; Intelligence is a target."""
    INT = {"signature": "suffix|additional_intelligence", "min": 43}
    REST = [{"signature": "prefix|base_maximum_life", "min": 90}, {"signature": "prefix|local_energy_shield", "min": 50}]

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.base = get_data().base_by_name("Vaal Regalia").id
        cls.Planner, cls.Prices = Planner, Prices

    def planner(self):
        return self.Planner(prices=self.Prices(use_live=False), sims=0)

    def test_the_essence_mod_is_always_there(self):
        r = self.planner().plan(self.base, 86, [], [self.INT], lang="en")
        s = next(s for s in r["strategies"] if str(s["name"]) == "Essence spam: Shrieking Essence of Spite")
        self.assertEqual(s["p_attempt"], 1.0)                       # one essence, never misses its own mod
        self.assertAlmostEqual(s["steps"][1]["attempts"], 1.0)

    def test_on_fail_never_says_nothing_worth_saving(self):
        r = self.planner().plan(self.base, 86, [], [self.INT] + self.REST, lang="en")
        self.assertEqual(str(r["recommended"]),
                         "Essence spam: Shrieking Essence of Spite + bench craft + Annul/metacraft recovery")
        best = r["strategies"][0]
        ess = next(st for st in best["steps"] if "Shrieking Essence of Spite" in st["title"])
        self.assertIn("Guaranteed: +(43-50) to Intelligence", ess["detail"])
        fails = [f["text"] for f in ess["on_fail"]]
        self.assertFalse(any("nothing worth saving" in t for t in fails), fails)
        restart = next(t for t in fails if "repeat this step" in t)
        self.assertIn("the guaranteed mod +# to Intelligence (>= 43) is there", restart)
        self.assertIn("puts +# to Intelligence (>= 43) back with every reforge", restart)
        # Exalt / Annul recovery is part of the plan where it pays - as a conditional branch of the essence step
        # (this used to look for steps titled "Recovery: ..." in the main list)
        branches = [st for st in best["steps"] if st.get("kind") == "recovery"]
        self.assertTrue(branches)
        self.assertTrue(all(st["title"].startswith("IF ") and st["branch_of"] == ess["number"] for st in branches))

    def test_policy_keeps_the_guaranteed_target_and_restarts_only_when_cheaper(self):
        caught = []
        planner = self.planner()
        orig = planner._recovery_variant

        def spy(ctx, targets, s):
            out = orig(ctx, targets, s)
            if out is not None and "essence" in s.params and "Spite" in s.params["essence"].name:
                caught.append((s, out))
            return out
        planner._recovery_variant = spy
        planner.plan(self.base, 86, [], [self.INT] + self.REST, lang=None)
        self.assertTrue(caught)
        for s, rec in caught:
            mdp = rec.params["mdp"]
            t_int = next(t for t in s.rng_targets if t.satisfied_by(s.params["mod"]))
            restart_v = mdp.restart_cost + mdp.vstart
            for _, i in mdp.d0:
                self.assertTrue(mdp.states[i][1] >> t_int.index & 1)      # Intelligence present after every essence
                if mdp.policy[i] is None and mdp.acts[i]:                 # "repeat the essence" here ...
                    best_action = min(a.cost + sum(p * (restart_v if j < 0 else mdp.V[j]) for p, j in outs)
                                      for a, outs in mdp.acts[i])
                    self.assertGreaterEqual(best_action, restart_v - 1e-6)  # ... because no Exalt/Annul is cheaper


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class FailTextAudit(unittest.TestCase):
    """On-fail / restart texts say what the item has and why the math chose the path (text audit)."""
    LIFE, ES = "prefix|base_maximum_life", "prefix|local_energy_shield"
    COLD, LIGHT = "suffix|base_cold_damage_resistance_%", "suffix|base_lightning_damage_resistance_%"
    WORKED = [{"signature": LIFE, "min": 140}, {"signature": ES, "min": 50}, {"signature": COLD, "min": 36},
              {"signature": LIGHT, "min": 36}]

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.base = get_data().base_by_name("Vaal Regalia").id
        cls.pl = Planner(prices=Prices(use_live=False), sims=0)
        cls.raw = cls.pl.plan(cls.base, 86, [], cls.WORKED, lang=None)
        cls.frac_raw = cls.pl.plan(cls.base, 86, [], [{"signature": cls.LIFE, "min": 140, "fractured": True},
                                                      {"signature": cls.COLD, "min": 36},
                                                      {"signature": cls.ES, "min": 50}], lang=None)

    @staticmethod
    def fails(result, name_part):
        s = next(s for s in result["strategies"] if name_part in str(s["name"]))
        return s, [f["text"] for st in s["steps"] for f in st.get("on_fail", [])]

    def test_no_nothing_worth_saving_anywhere(self):
        from poecraft.messages import render
        banned = {"en": "nothing worth saving", "ru": "сохранять нечего", "de": "nichts zu retten"}
        for raw in (self.raw, self.frac_raw):
            for lang, phrase in banned.items():
                out = render(raw, lang)
                texts = [f["text"] for s in out["strategies"] for st in s["steps"] for f in st.get("on_fail", [])]
                self.assertFalse([t for t in texts if phrase in t.lower()], lang)

    def test_essence_guaranteed_mod_is_named(self):
        from poecraft.messages import render
        en = render(self.raw, "en")
        _, texts = self.fails(en, "Screaming Essence of Hatred + bench craft + Annul")
        self.assertTrue(any("the guaranteed mod +#% to Cold Resistance (>= 36) is there" in t for t in texts))
        _, plain = self.fails(en, "Essence spam: Screaming Essence of Hatred + bench craft")
        ru = render(self.raw, "ru")
        _, ru_texts = self.fails(ru, "Кричащая сущность ненависти + ремесло верстака + восстановление")
        self.assertTrue(any("гарантированный модификатор" in t for t in ru_texts))

    def test_fossil_restart_says_how_many_items_had_targets(self):
        from poecraft.messages import render
        _, texts = self.fails(render(self.raw, "en"), "Fossil spam: Frigid + Metallic + Pristine + bench craft + Annul")
        partial = next(t for t in texts if t.startswith(tuple("0123456789")) and "not worth finishing" in t)
        self.assertRegex(partial, r"[\d.]+% of these items do have target mods, but adding the missing ones with "
                                  r"Exalted Orb / Orb of Annulment costs more here than another try")
        keep = next(t for t in texts if "these items are worth keeping" in t)
        self.assertRegex(keep, r"on average this saves ~[\d.,]+ c compared with starting over")

    def test_fracturing_texts_name_the_fractured_mod(self):
        from poecraft.messages import render
        en = render(self.frac_raw, "en")
        best = en["strategies"][0]
        texts = [f["text"] for st in best["steps"] for f in st.get("on_fail", [])]
        self.assertTrue(any(t.startswith("+# to maximum Life (>= 140) has not rolled yet: reroll again") for t in texts))
        self.assertTrue(any("a fractured modifier is permanent, so this base can no longer get "
                            "+# to maximum Life (>= 140) fractured" in t for t in texts))
        self.assertTrue(any("the fractured +# to maximum Life (>= 140) stays" in t and "the next essence adds" in t
                            for t in texts), texts)
        de = render(self.frac_raw, "de")
        de_texts = [f["text"] for st in de["strategies"][0]["steps"] for f in st.get("on_fail", [])]
        self.assertTrue(any("ein zerbrochener Modifikator ist dauerhaft" in t for t in de_texts))

    def test_alteration_scouring_is_explained(self):
        from poecraft.messages import render
        _, texts = self.fails(render(self.raw, "en"), "Alteration → Regal → Exalt + bench craft")
        self.assertTrue(any("Orb of Scouring (the rare item becomes Normal again) and go back to 'Make it magic'"
                            in t for t in texts))

    def test_have_restart_uses_scouring_only_when_the_fresh_plan_needs_normal(self):
        rec = self.pl.recover(self.base, 86, [], [{"signature": self.LIFE, "min": 160}, {"signature": self.COLD, "min": 42},
                                                  {"signature": self.LIGHT, "min": 25}],
                              ["IncreasedLife12", "ColdResist8", "LightningResist1"], lang="en")
        fresh = rec["fresh_plan"]
        texts = [st["action"] for st in rec["steps"]] + [f["text"] for st in rec["steps"] for f in st.get("on_fail", [])]
        give_up = next(t for t in texts if t.startswith(("If the policy gives up", "Keeping this item costs more")))
        if "Shrieking" in str(fresh["name"]) or "Screaming" in str(fresh["name"]) or "Chaos" in str(fresh["name"]):
            self.assertIn("on this item directly (it reforges a rare item, no Orb of Scouring needed)", give_up)
        self.assertNotIn("Nothing here is worth saving", " ".join(texts))


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class HubPages(unittest.TestCase):
    """'/' is the TensChep Workshop hub, '/craft' the planner; the brand is never translated."""
    BRAND = "TensChep Workshop"

    @classmethod
    def setUpClass(cls):
        import threading
        from poecraft import server
        cls.httpd = server.BoundedServer(("127.0.0.1", 0), server.Handler, 8)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def get(self, path):
        import http.client
        conn = http.client.HTTPConnection(self.base.split("//")[1], timeout=20)
        conn.request("GET", path)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read().decode("utf-8", "replace")
        conn.close()
        return out

    def test_root_is_the_hub(self):
        for path in ("/", "/hub"):
            code, headers, html = self.get(path)
            self.assertEqual(code, 200, path)
            self.assertIn("text/html", headers["Content-Type"])
            self.assertIn(f"<title>{self.BRAND}</title>", html)
            self.assertIn(f'<h1 class="brand-title">{self.BRAND}</h1>', html)     # plain text, no data-i18n
            self.assertIn('data-i18n="hub.tagline"', html)
            self.assertIn('<script src="/hub.js"></script>', html)
            self.assertNotRegex(html, r"<script>(?!</script>)")                     # no inline script (CSP)
        for asset, ctype in (("/hub.js", "javascript"), ("/hub.css", "text/css"), ("/style.css", "text/css")):
            code, headers, _ = self.get(asset)
            self.assertEqual(code, 200, asset)
            self.assertIn(ctype, headers["Content-Type"])

    def test_planner_moved_to_craft(self):
        code, _, html = self.get("/craft")
        self.assertEqual(code, 200)
        self.assertIn('<script src="/app.js"></script>', html)                      # absolute: works at /craft
        self.assertIn('<link rel="stylesheet" href="/style.css">', html)
        self.assertIn(f'href="/" data-i18n-title="nav.home">&larr; {self.BRAND}</a>', html)   # back to the hub
        code, headers, _ = self.get("/craft/")
        self.assertEqual((code, headers.get("Location")), (301, "/craft"))
        self.assertEqual(self.get("/app.js")[0], 200)

    def test_cards_come_from_the_tool_list_and_texts_are_localized(self):
        import json
        import re
        from pathlib import Path
        from poecraft import config as cfg, i18n
        js = (cfg.WEB_DIR / "hub.js").read_text(encoding="utf-8")
        self.assertIn(f'const BRAND = "{self.BRAND}";', js)
        tools = re.findall(r'\{id: "(\w+)", href: "([^"]+)"', js)
        self.assertEqual(tools, [("craft", "/craft"), ("evaluator", "/evaluator")])   # two cards (was: one)
        keys = ["hub.tagline", "hub.launch", "hub.more", "hub.footer"] + \
               [f"hub.tool.{t}.{part}" for t, _ in tools for part in ("title", "desc")]
        data = {c: json.loads((i18n.LOCALES_DIR / f"{c}.json").read_text(encoding="utf-8")) for c in ("en", "ru", "de")}
        for k in keys:
            for c in ("ru", "de"):
                self.assertIn(k, data[c], f"{c}: {k}")
                self.assertNotEqual(data[c][k], data["en"][k], f"{c}: {k} not translated")
        self.assertEqual(data["ru"]["hub.tagline"], "Инструменты для крафтеров Path of Exile")
        self.assertEqual(data["ru"]["hub.tool.craft.title"], "Планировщик крафта")
        self.assertEqual(data["ru"]["hub.launch"], "Запустить")
        # the brand is not a translatable string anywhere: no locale value carries it (or a variant of it)
        for c, d in data.items():
            self.assertFalse([k for k, v in d.items() if isinstance(v, str) and "tenschep" in v.lower()], c)


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class FindOnTradeEndToEnd(unittest.TestCase):
    """Regression: 'Find on Trade' failed with "'dict' object is not callable" - the search-link cache dict had
    taken the name of the disk-cache helper _cache() used by _get(). Only the network is faked here: the module's
    own code (disk cache, league and stat lookup, short link) runs for real."""
    STATS = {"result": [{"id": "explicit", "entries": [{"id": "explicit.stat_3299347043", "text": "+# to maximum Life"},
                                                       {"id": "explicit.stat_2250533757", "text": "#% increased Movement Speed"}]}]}
    LEAGUES = {"result": [{"id": "Allflame", "realm": "pc"}, {"id": "Standard", "realm": "pc"}]}

    def setUp(self):
        import tempfile
        from unittest import mock
        from poecraft import config as cfg
        from poecraft.data import trade
        trade._reset_limits()
        self.addCleanup(trade._reset_limits)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = mock.patch.object(cfg, "DATA_DIR", __import__("pathlib").Path(tmp.name))   # fresh disk cache
        p.start()
        self.addCleanup(p.stop)
        self.calls = []

        def fake_urlopen(req, timeout=None):
            import io
            import json
            url = req.full_url if hasattr(req, "full_url") else req
            self.calls.append(url)
            if url.endswith("/api/trade/data/leagues"):
                body = self.LEAGUES
            elif url.endswith("/api/trade/data/stats"):
                body = self.STATS
            elif "/api/trade/search/" in url:
                body = {"id": "H4sIAAAA-test_id", "result": []}
            else:
                raise AssertionError(url)
            return io.BytesIO(json.dumps(body).encode())
        p2 = mock.patch("urllib.request.urlopen", side_effect=fake_urlopen)
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch("poecraft.engine.tradelink._price_league", return_value="Allflame")
        p3.start()
        self.addCleanup(p3.stop)

    def test_trade_search_runs_its_real_lookups(self):
        from poecraft.data.repository import get_data
        from poecraft.engine.tradelink import trade_search
        base = get_data().base_by_name("Crusader Boots").id
        r = trade_search(base, 86, [], [{"signature": "prefix|base_maximum_life", "min": 70}])   # no TypeError
        self.assertEqual(r["url"], "https://www.pathofexile.com/trade/search/Allflame/H4sIAAAA-test_id")
        self.assertTrue(r["short"])
        self.assertEqual(r["filters"], [{"line": "+# to maximum Life", "id": "explicit.stat_3299347043", "min": 70}])
        from poecraft import config as cfg
        self.assertTrue((cfg.DATA_DIR / "trade_stats.json").exists())            # the disk cache was written
        n = len(self.calls)
        trade_search(base, 86, [], [{"signature": "prefix|base_maximum_life", "min": 70}])
        self.assertEqual(len(self.calls), n)          # second time: stats/leagues from disk, link from the cache

    def test_api_trade_endpoint_returns_a_link(self):
        import http.client
        import json
        import threading
        from poecraft import server
        from poecraft.data.repository import get_data
        httpd = server.BoundedServer(("127.0.0.1", 0), server.Handler, 4)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
            body = {"lang": "ru", "base": get_data().base_by_name("Crusader Boots").id, "ilvl": 86, "influences": [],
                    "league": None, "targets": [{"signature": "prefix|base_maximum_life", "key": "prefix|base_maximum_life",
                                                 "label": "+# to maximum Life", "min": 70}]}
            conn.request("POST", "/api/trade", json.dumps(body), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            out = json.loads(resp.read())
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(resp.status, 200, out)
        self.assertNotIn("error", out)
        self.assertEqual(out["url"], "https://www.pathofexile.com/trade/search/Allflame/H4sIAAAA-test_id")
        self.assertEqual(out["league"], "Allflame")


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class ItemEvaluator(unittest.TestCase):
    """Item Evaluator, offline: the trade site is faked; parsing, ranking and relaxation run for real."""
    BOOTS = """Item Class: Boots
Rarity: Rare
Doom Stride
Crusader Boots
--------
Armour: 139
Energy Shield: 28
--------
Item Level: 86
--------
+82 to maximum Life
25% increased Movement Speed
+40% to Fire Resistance
+38% to Lightning Resistance
+48 to maximum Mana (crafted)
"""
    STATS = {"explicit": {"+# to maximum Life": "s.life", "#% increased Movement Speed": "s.ms",
                          "+#% to Fire Resistance": "s.fire", "+#% to Lightning Resistance": "s.light"},
             "crafted": {"+# to maximum Mana": "c.mana"}, "implicit": {}, "fractured": {}}

    def setUp(self):
        from poecraft.data import trade
        from poecraft.engine import evaluator
        evaluator._CACHE.clear()
        trade._reset_limits()
        self.searches = []

    def run_eval(self, text, found=lambda payload, n: 0, rows=None, lang="en"):
        from unittest import mock
        from poecraft.data import trade
        from poecraft.engine import evaluator
        from poecraft.messages import render

        def fake_search(league, payload, wait=20.0):
            self.searches.append(payload)
            total = found(payload, len(self.searches))
            return {"id": f"q{len(self.searches)}", "total": total, "result": [f"r{i}" for i in range(total)]}

        def fake_fetch(qid, ids, wait=20.0):
            return rows if rows is not None else [
                {"listing": {"price": {"amount": 40, "currency": "chaos"}, "account": {"online": {"league": "L"}}},
                 "item": {"typeLine": "Crusader Boots", "ilvl": 84, "explicitMods": ["+80 to maximum Life"]}}]
        with mock.patch.object(trade, "search_ids", fake_search), mock.patch.object(trade, "fetch_listings", fake_fetch), \
                mock.patch.object(trade, "stat_index", lambda: self.STATS), \
                mock.patch.object(trade, "leagues", lambda realm="pc": ["Allflame", "Standard"]), \
                mock.patch("poecraft.engine.evaluator._price_league", return_value="Allflame"):
            return render(evaluator.evaluate(text), lang)

    @staticmethod
    def filters(payload):
        q = payload["query"]
        stats = [f["id"] for f in q["stats"][0]["filters"]] if q.get("stats") else []
        misc = list(q.get("filters", {}).get("misc_filters", {}).get("filters", {}))
        return stats + misc

    def test_relaxation_drops_cheapest_to_recreate_first_and_keeps_the_core(self):
        from poecraft import config as cfg
        r = self.run_eval(self.BOOTS)                                   # never finds anything
        self.assertEqual(r["kind"], "rare")
        self.assertEqual(r["league"], "Allflame")
        first = self.filters(self.searches[0])
        self.assertEqual(sorted(first), sorted(["s.life", "s.ms", "s.fire", "s.light", "c.mana", "ilvl"]))
        self.assertLessEqual(len(self.searches), cfg.EVAL_MAX_SEARCHES)  # the search budget holds
        # each relaxed search keeps a subset of the previous one; item level and the crafted mod go first
        sets = [set(self.filters(p)) for p in self.searches[:-1]]
        self.assertTrue(all(b <= a for a, b in zip(sets, sets[1:])))
        self.assertNotIn("ilvl", sets[1])
        # the order things were dropped in: item level, then the bench-craftable Mana (cheapest to recreate)
        order = []
        for p in r["passes"]:
            order += [d for d in p["dropped"] if d not in order]
        self.assertEqual(order[:2], ["item level ≥ 86", "+48 to maximum Mana"])
        core = [e["label"] for e in r["ranking"] if e["core"]]
        self.assertTrue(core)
        for label in core:                                           # the core is never dropped
            self.assertTrue(all(label not in p["dropped"] for p in r["passes"]))
        mana = next(e for e in r["ranking"] if e["label"] == "+48 to maximum Mana")
        self.assertIn("bench-craft", mana["why"])
        self.assertEqual(r["passes"][-1]["status"], "any")             # nothing online: one search incl. offline
        self.assertTrue(any(w.startswith("No listings found") for w in r["warnings"]))
        # the importer's notes about the planner's "have" mode are not shown in a price check
        self.assertFalse([w for w in r["warnings"] if w.startswith("Crafted modifier imported as a target")])

    def test_stops_as_soon_as_something_is_found(self):
        r = self.run_eval(self.BOOTS, found=lambda p, n: 0 if n == 1 else 3)
        self.assertEqual(len(self.searches), 2)
        self.assertEqual(r["passes"][1]["dropped"], ["item level ≥ 86"])
        self.assertEqual(len(r["listings"]), 1)

    def test_listings_online_first_then_cheapest_in_chaos(self):
        rows = [{"listing": {"price": {"amount": a, "currency": c}, "account": {"online": {"league": "L"} if on else None}},
                 "item": {"typeLine": "Crusader Boots", "ilvl": 84, "explicitMods": [{"description": "+80 to maximum Life"}]}}
                for a, c, on in ((2, "divine", True), (40, "chaos", True), (5, "chaos", False), (90, "chaos", True))]
        r = self.run_eval(self.BOOTS, found=lambda p, n: 4, rows=rows)
        order = [(l["online"], l["price"]["amount"], l["price"]["currency"]) for l in r["listings"]]
        self.assertEqual(order[:3], [(True, 40, "chaos"), (True, 90, "chaos"), (True, 2, "divine")])
        self.assertEqual(order[3], (False, 5, "chaos"))                # offline last even though cheapest
        # the summary: cheapest ONLINE listing, and the trade site's count of online listings
        self.assertEqual((r["summary"]["price"]["amount"], r["summary"]["price"]["currency"]), (40, "chaos"))
        self.assertEqual((r["summary"]["online"], r["summary"]["offline_only"]), (4, False))
        self.assertGreater(r["listings"][2]["chaos"], 90)               # divines converted to chaos
        self.assertEqual(r["listings"][0]["mods"], ["+80 to maximum Life"])

    def test_offline_fallback_when_no_online_seller(self):
        rows = [{"listing": {"price": {"amount": 25, "currency": "chaos"}, "account": {"online": None}},
                 "item": {"typeLine": "Crusader Boots", "ilvl": 84, "explicitMods": []}}]
        r = self.run_eval(self.BOOTS, found=lambda p, n: 2 if p["query"]["status"]["option"] == "any" else 0, rows=rows)
        self.assertEqual((r["summary"]["offline_only"], r["summary"]["online"]), (True, 0))
        self.assertEqual(r["passes"][-1]["status"], "any")
        self.assertTrue(any(w.startswith("No online sellers found") for w in r["warnings"]))
        self.assertTrue(r["listings"])

    LETHAL_PRIDE = """Item Class: Jewels
Rarity: Unique
Lethal Pride
Timeless Jewel
--------
Limited to: 1 Historic
Radius: Large
--------
Item Level: 84
--------
Commanded leadership over 12000 warriors under Kaom
Passives in radius are Conquered by the Karui
Historic
"""

    def test_timeless_jewel_is_searched_by_conqueror_and_seed(self):
        rows = [{"listing": {"price": {"amount": 30, "currency": "chaos"}, "account": {"online": {"league": "L"}}},
                 "item": {"name": "Lethal Pride", "typeLine": "Timeless Jewel", "ilvl": 84, "explicitMods": []}}]
        r = self.run_eval(self.LETHAL_PRIDE, found=lambda p, n: 3, rows=rows)
        self.assertEqual(r["kind"], "timeless")
        t = r["timeless"]
        self.assertEqual((t["jewel"], t["conqueror"], t["seed"], t["version"]), ("Lethal Pride", "Kaom", 12000, 2))
        self.assertEqual(t["seed_range"], [10000, 18000])
        q = self.searches[0]["query"]
        self.assertEqual((q["name"], q["type"]), ("Lethal Pride", "Timeless Jewel"))
        self.assertEqual(q["stats"][0]["filters"], [{"id": "explicit.pseudo_timeless_jewel_kaom",
                                                     "value": {"min": 12000, "max": 12000}}])
        self.assertEqual((r["summary"]["price"]["amount"], r["summary"]["online"]), (30, 3))
        self.assertNotIn("calc", t)                                    # effects need a socket

    def test_timeless_without_seed_line_says_so(self):
        r = self.run_eval("Item Class: Jewels\nRarity: Unique\nLethal Pride\nTimeless Jewel\n--------\nItem Level: 84\n")
        self.assertEqual(r["kind"], "timeless")
        self.assertTrue(r["message"].startswith("This looks like a Timeless Jewel, but its seed line"))
        self.assertEqual(self.searches, [])
        self.assertIsNone(r["summary"])

    def test_unique_by_name_and_russian_unique_by_base(self):
        r = self.run_eval("Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------\nItem Level: 80\n",
                          found=lambda p, n: 5)
        q = self.searches[0]["query"]
        self.assertEqual((r["kind"], q.get("name"), q["type"]), ("unique", "Sin Trek", "Stealth Boots"))
        from poecraft import i18n
        from poecraft.data.repository import get_data
        b = get_data().base_by_name("Stealth Boots")
        ru_base = i18n.game_text("base", b.id, b.name, "ru")[0]
        self.searches.clear()
        from poecraft.engine import evaluator
        evaluator._CACHE.clear()
        r = self.run_eval(f"Класс предмета: Ботинки\nРедкость: Уникальный\nПуть греха\n{ru_base}\n--------\nУровень предмета: 80\n",
                          found=lambda p, n: 5)
        q = self.searches[0]["query"]
        self.assertNotIn("name", q)                                     # the Russian unique name cannot be mapped
        self.assertEqual(q["type"], "Stealth Boots")
        self.assertEqual(q["filters"]["type_filters"]["filters"]["rarity"]["option"], "unique")
        self.assertTrue(any("could not be translated" in w for w in r["warnings"]))

    def test_tincture_is_searched_by_its_base_type(self):
        r = self.run_eval("Item Class: Tinctures\nRarity: Magic\nConcentrated Prismatic Tincture of Empowering\n--------\n"
                          "Inflicts Mana Burn every 0.7 seconds\n--------\nItem Level: 83\n--------\n"
                          "70% increased Elemental Damage with Melee Weapons (implicit)\n--------\n32% increased effect\n",
                          found=lambda p, n: 2)
        self.assertNotEqual(r["kind"], "unknown")
        self.assertEqual(self.searches[0]["query"]["type"], "Prismatic Tincture")
        self.assertTrue(any(w.startswith("Searched by base type (Prismatic Tincture)") for w in r["warnings"]))

    def test_unique_prismatic_jewels_are_searched_by_name(self):
        for name in ("Watcher's Eye", "The Light of Meaning"):
            self.searches.clear()
            from poecraft.engine import evaluator
            evaluator._CACHE.clear()
            r = self.run_eval(f"Item Class: Jewels\nRarity: Unique\n{name}\nPrismatic Jewel\n--------\nLimited to: 1\n"
                              f"--------\nItem Level: 86\n--------\n4% increased maximum Life\n", found=lambda p, n: 4)
            q = self.searches[0]["query"]
            self.assertEqual((r["kind"], q.get("name"), q.get("type")), ("unique", name, "Prismatic Jewel"))
            self.assertEqual(q["filters"]["type_filters"]["filters"]["rarity"]["option"], "unique")
            self.assertFalse(q.get("stats"))                            # by name only, no mod filters

    def test_crafted_hybrid_in_stat_order_on_a_helmet(self):
        from poecraft.data.repository import get_data
        from poecraft.engine.itemtext import parse_item_text
        text = ("Item Class: Helmets\nRarity: Rare\nGrim Visage\nConqueror's Helmet\n--------\nItem Level: 85\n--------\n"
                "+93 to maximum Life\n+1 to Level of Socketed AoE Gems (crafted)\n8% increased Area of Effect (crafted)\n")
        p = parse_item_text(text, get_data())
        hybrid = [t for t in p["targets"] if "area_of_effect_gem_level" in t["key"]]
        self.assertEqual(len(hybrid), 1)                                 # the two lines are one crafted mod
        self.assertFalse([w for w in p["warnings"] if "skipped" in str(w)])
        line = next(l for l in p["lines"] if "AoE" in l["text"])
        self.assertEqual((line["kind"], line["matched"]), ("crafted", True))

    def test_white_base_is_priced_by_type_and_item_level(self):
        text = ("Item Class: Body Armours\nRarity: Normal\nAstral Plate\n--------\nArmour: 711\n--------\nItem Level: 84\n"
                "--------\n+12% to all Elemental Resistances (implicit)\n")
        r = self.run_eval(text, found=lambda p, n: 5)
        q = self.searches[0]["query"]
        self.assertEqual((r["kind"], q["type"]), ("base", "Astral Plate"))
        self.assertEqual(q["filters"]["misc_filters"]["filters"]["ilvl"], {"min": 84})
        self.assertTrue(r["summary"])

    def test_corrupted_and_mirrored_items_are_compared_with_their_kind(self):
        for status, line in (("corrupted", "Corrupted"), ("mirrored", "Mirrored"), ("corrupted", "Осквернено"),
                             ("corrupted", "Verderbt")):
            from poecraft.engine import evaluator
            evaluator._CACHE.clear()
            self.searches.clear()
            r = self.run_eval(self.BOOTS + f"--------\n{line}\n", found=lambda p, n: 2)
            for p in self.searches:
                self.assertEqual(p["query"]["filters"]["misc_filters"]["filters"][status], {"option": "true"})
            self.assertTrue(any(w.startswith("Compared with") for w in r["warnings"]), line)
        evaluator._CACHE.clear()
        self.searches.clear()
        self.run_eval(self.BOOTS, found=lambda p, n: 2)                  # a clean item: no state filter
        self.assertNotIn("corrupted", self.searches[0]["query"].get("filters", {}).get("misc_filters", {}).get("filters", {}))

    def test_unique_with_no_listings_says_so(self):
        r = self.run_eval("Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------\nItem Level: 80\n")
        self.assertEqual(r["kind"], "unique")
        self.assertIsNone(r["summary"])
        self.assertEqual([p["status"] for p in r["passes"]], ["online", "any"])
        self.assertTrue(any(w.startswith("Nothing listed on the trade site") for w in r["warnings"]))

    def test_rare_with_only_influence_modifiers(self):
        from poecraft.data.repository import get_data
        data = get_data()
        hunter = next(m for m in data.mods.values() if m.text and "\n" not in m.text and m.affix == "suffix"
                      and any(t == "boots_basilisk" and w > 0 for t, w in m.spawn_weights))
        line = re.sub(r"\((\d+)-\d+\)", r"\1", hunter.text)
        stats = dict(self.STATS, explicit=dict(self.STATS["explicit"], **{re.sub(r"\d+", "#", line): "s.hunter"}))
        text = (f"Item Class: Boots\nRarity: Rare\nGale Road\nTwo-Toned Boots\n--------\nItem Level: 86\n--------\n"
                f"+12% to Fire and Cold Resistances (implicit)\n--------\n{line}\n--------\nHunter Item\n")
        with unittest.mock.patch.object(self, "STATS", stats):
            r = self.run_eval(text)                                     # never finds anything
        self.assertEqual(r["kind"], "rare")
        first = self.filters(self.searches[0])
        self.assertIn("hunter_item", first)
        self.assertNotIn("+12% to Fire and Cold Resistances", [e["label"] for e in r["ranking"]])  # the base implicit
        core = [e["label"] for e in r["ranking"] if e["core"]]
        self.assertEqual(len(core), 1)                                   # the only modifier is the core
        self.assertTrue(all(core[0] not in p["dropped"] for p in r["passes"]))

    def test_regular_jewel_is_not_a_timeless_jewel(self):
        from poecraft.engine import timeless as tj
        text = ("Item Class: Jewels\nRarity: Rare\nWarriors' Spark\nCobalt Jewel\n--------\nItem Level: 80\n--------\n"
                "7% increased maximum Life\n+12 to Strength\nCommanded 12000 warriors\n")
        self.assertIsNone(tj.parse(text))
        r = self.run_eval(text, found=lambda p, n: 1)
        self.assertNotEqual(r["kind"], "timeless")
        self.assertTrue(any(w.startswith("Searched by base type (Cobalt Jewel)") for w in r["warnings"]))

    def test_trade_site_down_is_a_message_not_a_crash(self):
        import urllib.error
        from unittest import mock
        from poecraft.data import trade
        from poecraft.engine import evaluator
        from poecraft.messages import render

        def down(*a, **k):
            raise urllib.error.URLError("Name or service not known")
        with mock.patch("urllib.request.urlopen", down), mock.patch.object(trade, "stat_index", lambda: self.STATS), \
                mock.patch("poecraft.engine.evaluator._price_league", return_value="Allflame"):
            r = render(evaluator.evaluate(self.BOOTS, league="Allflame"), "ru")
        self.assertEqual(r["listings"], [])
        self.assertTrue(any("торговая площадка недоступна" in w for w in r["warnings"]), r["warnings"])

    def test_exarch_implicit_on_gloves_is_kept_and_searched_as_an_implicit(self):
        from poecraft.data.repository import get_data
        from poecraft.engine.itemtext import parse_item_text
        from poecraft.messages import render
        stats = dict(self.STATS, implicit={"#% increased Fire Damage": "implicit.stat_3962278098"})
        for text in ("Item Class: Gloves\nRarity: Rare\nX\nWarlock Gloves\n--------\nItem Level: 84\n--------\n"
                     "{ Searing Exarch Implicit Modifier (Lesser) — Damage, Elemental, Fire }\n10% increased Fire Damage\n"
                     "--------\n+70 to maximum Life\n--------\nSearing Exarch Item\n",
                     "Item Class: Gloves\nRarity: Rare\nX\nWarlock Gloves\n--------\nItem Level: 84\n--------\n"
                     "10% increased Fire Damage (implicit)\n--------\n+70 to maximum Life\n--------\nSearing Exarch Item\n"):
            p = parse_item_text(text, get_data())
            en = [render(w, "en") for w in p["warnings"]]
            self.assertFalse([w for w in en if "cannot have it" in w or "skipped" in w], en)     # not "impossible"
            self.assertEqual(p["extra_implicits"][0]["side"], "exarch")
            self.assertEqual(p["extra_implicits"][0]["english"], "#% increased Fire Damage")
            from poecraft.engine import evaluator
            evaluator._CACHE.clear()
            self.searches.clear()
            with unittest.mock.patch.object(self, "STATS", stats):
                r = self.run_eval(text, found=lambda p, n: 1)
            first = self.searches[0]["query"]["stats"][0]["filters"]
            self.assertIn({"id": "implicit.stat_3962278098", "value": {"min": 9}}, first)
            self.assertIn("10% increased Fire Damage", [e["label"] for e in r["ranking"]])

    def test_notes_are_quiet_info_with_copyable_lines(self):
        gloves = ("Item Class: Gloves\nRarity: Rare\nX\nWarlock Gloves\n--------\nItem Level: 84\n--------\n"
                  "{ Searing Exarch Implicit Modifier (Lesser) }\n10% increased Fire Damage\n--------\n"
                  "+70 to maximum Life\n--------\nSearing Exarch Item\n")
        r = self.run_eval(gloves, found=lambda p, n: 1)
        self.assertTrue(r["notes"])
        self.assertTrue(all(n["level"] == "info" for n in r["notes"]))        # missing data is not an error
        eld = next(n for n in r["notes"] if n["copy"])
        self.assertEqual(eld["copy"], "10% increased Fire Damage")             # "Add as trade filter manually"
        self.assertEqual(len(r["notes"]), len(r["warnings"]))
        from poecraft.engine import evaluator
        evaluator._CACHE.clear()
        t = self.run_eval("Item Class: Tinctures\nRarity: Magic\nConcentrated Prismatic Tincture of Empowering\n--------\n"
                          "Item Level: 83\n", found=lambda p, n: 1)
        note = next(n["text"] for n in t["notes"] if "Prismatic Tincture" in n["text"])
        self.assertIn("Tincture mods aren't filtered automatically - use the base search below", note)

    def test_trade_failure_is_an_error_note(self):
        import urllib.error
        from unittest import mock
        from poecraft.data import trade
        from poecraft.engine import evaluator
        from poecraft.messages import render

        def down(*a, **k):
            raise urllib.error.URLError("down")
        with mock.patch("urllib.request.urlopen", down), mock.patch.object(trade, "stat_index", lambda: self.STATS), \
                mock.patch("poecraft.engine.evaluator._price_league", return_value="Allflame"):
            r = render(evaluator.evaluate(self.BOOTS, league="Allflame"), "en")
        self.assertEqual([n["level"] for n in r["notes"] if "trade site" in n["text"].lower()][:1], ["error"])

    def test_currency_in_russian_maps_to_the_english_name(self):
        r = self.run_eval("Класс предмета: Валюта\nРедкость: Валюта\nБожественная сфера\n--------\nРазмер стопки: 3/20\n",
                          found=lambda p, n: 1)
        self.assertEqual(r["kind"], "currency")
        self.assertEqual(self.searches[0]["query"]["type"], "Divine Orb")

    def test_results_are_cached_for_five_minutes(self):
        self.run_eval(self.BOOTS, found=lambda p, n: 3)
        n = len(self.searches)
        r = self.run_eval(self.BOOTS, found=lambda p, n: 3)
        self.assertEqual(len(self.searches), n)                        # no new trade searches
        self.assertTrue(r.get("cached"))

    def test_limiter_reads_ggg_state_and_waits_or_refuses(self):
        import time
        from poecraft.data import trade
        trade._note_state("fetch", {"X-Rate-Limit-Ip": "12:4:10,16:12:300", "X-Rate-Limit-Ip-State": "11:4:0,3:12:0"})
        self.assertGreater(trade._paused["fetch"], time.monotonic())   # one away from the 4 s maximum: pause
        with self.assertRaises(trade.TradeError):
            trade._slot("fetch", wait=0.0)
        trade._reset_limits()
        trade._slot("fetch", wait=0.0)                                  # free again

    def test_api_evaluate_endpoint(self):
        import http.client
        import json
        import threading
        from unittest import mock
        from poecraft import server
        from poecraft.engine import evaluator
        fake = {"kind": "timeless", "league": "Allflame", "league_source": "trade", "ranking": [], "passes": [],
                "listings": [], "search_url": None, "warnings": [], "message": "x"}
        httpd = server.BoundedServer(("127.0.0.1", 0), server.Handler, 4)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(evaluator, "evaluate", return_value=fake):
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
                conn.request("POST", "/api/evaluate", json.dumps({"text": "x", "lang": "en"}), {"Content-Type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.request("GET", "/evaluator")
                page = conn.getresponse()
                html = page.read().decode()
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual((resp.status, body["kind"]), (200, "timeless"))
        self.assertEqual(page.status, 200)
        self.assertIn('<script src="/evaluator.js"></script>', html)
        self.assertIn("TensChep Workshop", html)


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class EldritchParsingAndBaseChecks(unittest.TestCase):
    """Warlock Boots (Energy Shield only) with a faster-ignite Eldritch implicit and an Armour/ES bench craft."""
    RU = """Класс предмета: Ботинки
Редкость: Редкий
Шаг рока
Сапоги колдуна
--------
Энергетический щит: 120
--------
Уровень предмета: 84
--------
{ Собственное свойство Пламенного экзарха (Мелкий) — Урон, Стихийный, Огонь, Состояние }
Наложенные вами поджоги наносят урон на 5% быстрее
--------
{ Ремесленное Свойство-префикс "Улучшенный"  — Здоровье, Защита, Броня, Энергетический щит }
18(18-21)% повышение брони и энергетического щита
+13(12-14) к максимуму здоровья
--------
Предмет Пламенного экзарха
"""

    def parse(self, text):
        from poecraft.data.repository import get_data
        from poecraft.engine.itemtext import parse_item_text
        return parse_item_text(text, get_data())

    def test_implicit_on_the_other_side_is_read_not_dropped(self):
        from poecraft.messages import render
        p = self.parse(self.RU)
        self.assertEqual(p["base_name"], "Warlock Boots")
        eld = [t for t in p["targets"] if t.get("eldritch")]
        self.assertEqual(len(eld), 1)                       # was: silently dropped
        self.assertEqual((eld[0]["eldritch"], eld[0]["min"]), ("eater", 5))   # the game data's side
        en = [render(w, "en") for w in p["warnings"]]
        self.assertTrue(any("the item says Searing Exarch" in w and "Eater of Worlds" in w for w in en))

    def test_impossible_craft_names_the_base_conflict(self):
        from poecraft.messages import render
        p = self.parse(self.RU)
        self.assertFalse([t for t in p["targets"] if "armour_and_energy_shield" in t["key"]])   # still impossible
        en = [render(w, "en") for w in p["warnings"]]
        hit = [w for w in en if w.startswith("Warlock Boots has only Energy Shield")]
        self.assertEqual(len(hit), 1)
        self.assertIn("needs Armour", hit[0])
        self.assertIn("Check the copied text", hit[0])
        ru = [render(w, "ru") for w in p["warnings"]]
        self.assertTrue(any("«Сапоги колдуна»" in w and "Броня" in w for w in ru))

    def test_presence_upgrade_and_fuzzy_text(self):
        from poecraft.messages import render
        base = "Item Class: Boots\nRarity: Rare\nX\nWarlock Boots\n--------\nItem Level: 84\n--------\n"
        p = self.parse(base + "Ignites you inflict deal Damage 5% faster (implicit)\n"
                       "While a Unique Enemy is in your Presence, Ignites you inflict deal Damage 9% faster (implicit)\n"
                       "--------\n+70 to maximum Life\n--------\nEater of Worlds Item\n")
        self.assertEqual(len([t for t in p["targets"] if t.get("eldritch")]), 1)       # the upgrade is part of it
        self.assertEqual([l["kind"] for l in p["lines"]], ["implicit", "presence", "explicit"])
        self.assertFalse([w for w in p["warnings"] if render(w, "en").startswith("Duplicate")])
        p = self.parse(base + "{ Eater of Worlds Implicit Modifier (Lesser) }\nIgnites you inflict deal damage 5 % faster.\n")
        self.assertEqual([t["key"] for t in p["targets"]], ["eater|faster_burn_%"])
        self.assertTrue(any(render(w, "en").startswith("Matched approximately") for w in p["warnings"]))
        # a plain base implicit on a non-Eldritch item is never fuzzy-matched to an Eldritch one
        p = self.parse("Item Class: Boots\nRarity: Rare\nX\nTwo-Toned Boots\n--------\nItem Level: 84\n--------\n"
                       "+12% to Fire and Lightning Resistances (implicit)\n--------\n+70 to maximum Life\n")
        self.assertFalse([t for t in p["targets"] if t.get("eldritch")])

    def test_defence_lines_that_contradict_the_base(self):
        from poecraft.messages import render
        p = self.parse("Item Class: Boots\nRarity: Rare\nX\nWarlock Boots\n--------\nArmour: 300\n--------\n"
                       "Item Level: 84\n--------\n+70 to maximum Life\n")
        self.assertTrue(any(render(w, "en").startswith("The item's defence lines (Armour) do not match Warlock Boots")
                            for w in p["warnings"]))
        p = self.parse(self.RU)                             # Energy Shield lines on an ES base: no such warning
        self.assertFalse([w for w in p["warnings"] if render(w, "en").startswith("The item's defence lines")])


class TimelessConverters(unittest.TestCase):
    """The download converters and the English fallback reader - no Timeless data needed."""

    def test_lua_table(self):
        from poecraft.data.timeless import lua_table
        d = lua_table('-- header\nreturn {\n\t["nodes"] = {\n\t\t[1] = { ["id"] = "a", ["sd"] = { [1] = "+5 to \\"X\\"", }, '
                      '["ks"] = true, ["n"] = -1.5 },\n\t},\n}')
        self.assertEqual(d, {"nodes": [{"id": "a", "sd": ['+5 to "X"'], "ks": True, "n": -1.5}]})

    def test_convert_index(self):
        from poecraft.data.timeless import convert_index
        idx = convert_index('nodeIDList = { }\nnodeIDList["size"] = 3\nnodeIDList["sizeNotable"] = 2\n'
                            'nodeIDList[6] = { index = 0, size = 10 }\nnodeIDList[529] = { index = 1, size = 12 }\n'
                            'nodeIDList["localIdToGlobalId"][2][5] = 400\nreturn nodeIDList')
        self.assertEqual(idx, {"size": 3, "notables": 2, "nodes": {"6": [0, 10], "529": [1, 12]},
                               "local_to_global": {"2": {"5": 400}}})

    def test_seed_templates_and_tree_positions(self):
        from poecraft.data.timeless import node_position, seed_templates
        entry = {"ids": ["local_unique_jewel_alternate_tree_version", "local_unique_jewel_alternate_tree_seed",
                         "local_unique_jewel_alternate_tree_keystone", "local_unique_jewel_alternate_tree_internal_revision"],
                 "Russian": [{"condition": [{"min": 2, "max": 2}, {}, {"min": 3, "max": 3}, {"min": 1}],
                              "string": "Повелевает {1} воинами Акойи\nПассивные умения в радиусе завоеваны каруи"}]}
        self.assertEqual(seed_templates([entry], "ru"),
                         [{"version": 2, "keystone": 3, "revision": 1, "line": "Повелевает {1} воинами Акойи"}])
        radii, per = [0, 82, 162, 335, 493, 662, 846], [1, 6, 16, 16, 40, 72, 72]
        x, y = node_position({"x": 0, "y": 0}, 2, 2, radii, per)          # orbit 2, index 2 = 45 degrees
        self.assertAlmostEqual(x, 162 * 0.7071, places=1)
        self.assertAlmostEqual(y, -162 * 0.7071, places=1)

    def test_english_reader_works_without_the_data(self):
        from unittest import mock
        from poecraft.data import timeless as td
        from poecraft.engine import timeless as tj
        with mock.patch.object(td, "available", return_value=False):
            info = tj.parse("Elegant Hubris\nTimeless Jewel\n--------\nCommissioned 116820 coins to commemorate Caspiro\n")
        self.assertEqual((info["version"], info["seed"], info["conqueror"]), (5, 116820, "Caspiro"))
        self.assertTrue(tj.valid_seed(5, 116820))
        self.assertFalse(tj.valid_seed(5, 116821))                       # Elegant Hubris: multiples of 20
        self.assertFalse(tj.valid_seed(2, 9999))
        self.assertIsNone(tj.parse("Item Class: Boots\nRarity: Rare\n"))
        with mock.patch.object(td, "available", return_value=False):      # advanced copy: "12000(10000-18000)"
            info = tj.parse("{ Unique Modifier }\nCommanded leadership over 12000(10000-18000) warriors under Kaom\n")
        self.assertEqual((info["seed"], info["conqueror"]), (12000, "Kaom"))

    @unittest.skipUnless((config.DATA_DIR / "timeless" / "manifest.json").exists(), "Timeless data not downloaded")
    def test_russian_advanced_copy_and_yo(self):
        from poecraft.engine import timeless as tj
        info = tj.parse("{ Уникальное свойство }\nОмыт в крови 2000(100-8000) жертв во имя Шибаквы\n")
        self.assertEqual((info["version"], info["seed"], info["conqueror"]), (1, 2000, "Xibaqua"))
        info = tj.parse("Выточен во славу 5000 послушников, обращенных Верховным жрецом Аварием\n")   # е for ё
        self.assertEqual((info["version"], info["seed"], info["conqueror"]), (4, 5000, "Avarius"))


@unittest.skipUnless((config.DATA_DIR / "timeless" / "manifest.json").exists(),
                     "Timeless data not downloaded - run `python -m poecraft timeless update`")
class TimelessCalculator(unittest.TestCase):
    """Against in-game results (the expectations in Vilsol/timeless-jewels' jewel_test.go, checked in game there)."""

    @classmethod
    def setUpClass(cls):
        from poecraft.data import timeless as td
        cls.tree = td.tree()
        cls.ids = {n["name"]: nid for nid, n in cls.tree["nodes"].items() if n["kind"] in ("notable", "keystone")}

    def effect(self, version, seed, conqueror, name):
        from poecraft.engine import timeless as tj
        nid = self.ids[name]
        return tj.node_effect(version, seed, conqueror, nid, self.tree["nodes"][nid])

    def test_glorious_vanity_seed_2000_xibaqua(self):
        e = self.effect(1, 2000, "Xibaqua", "Instability")              # vaal_notable_block_2, rolls 8 / 22
        self.assertEqual((e["change"], e["new_name"]), ("replaced", "Thaumaturgical Protection"))
        self.assertEqual(e["new"], ["8% Chance to Block Spell Damage", "22% increased Defences from Equipped Shield"])
        e = self.effect(1, 2000, "Xibaqua", "Eagle Eye")                # Legacy of the Vaal: additions 14 / 23 / 36
        self.assertEqual(e["new_name"], "Legacy of the Vaal")
        self.assertEqual(e["new"], ["+6% to Critical Strike Multiplier", "5% increased maximum Mana",
                                    "+11% to Lightning Resistance"])

    def test_lethal_pride_seed_12000_kaom(self):
        e = self.effect(2, 12000, "Kaom", "Instability")                 # addition 42, roll 20
        self.assertEqual((e["change"], e["added"]), ("added", ["20% increased Armour"]))
        e = self.effect(2, 12000, "Kaom", "Eagle Eye")                   # addition 57
        self.assertEqual(e["added"], ["12% increased Totem Placement speed"])

    def test_keystones_follow_the_conqueror_including_revised_ones(self):
        self.assertEqual(self.effect(2, 12000, "Kaom", "Supreme Ego")["new_name"], "Strength of Blood")
        self.assertEqual(self.effect(2, 12000, "Akoya", "Supreme Ego")["new_name"], "Chainbreaker")      # 3_v2
        self.assertEqual(self.effect(1, 2000, "Ahuana", "Supreme Ego")["new_name"], "Immortal Ambition")  # 2_v2

    def test_every_socket_and_conqueror_resolves(self):
        from poecraft.data import timeless as td
        from poecraft.engine import timeless as tj
        socks = tj.sockets()
        self.assertEqual(len(socks), 21)
        for version, conqs in tj.CONQUERORS.items():
            lo, _ = td.SEEDS[version]
            seed = (lo + 11) * td.SEED_DIVISOR.get(version, 1)
            for c in conqs:
                r = tj.calculate(version, seed, c, socks[(version * 3) % 21]["id"])
                self.assertEqual(r["missing"], 0, (version, c))
                self.assertTrue(all(e["change"] != "unchanged" for e in r["nodes"] if e["kind"] != "normal" or version != 1))
        with self.assertRaises(ValueError):
            tj.calculate(2, 9999, "Kaom", socks[0]["id"])

    def test_equivalent_seeds_match_the_must_have_notables(self):
        from poecraft.data import timeless as td
        from poecraft.engine import timeless as tj
        sock = next(s["id"] for s in tj.sockets() if "Instability" in s["notables"])
        inst = self.ids["Instability"]
        r = tj.equivalents(2, 12000, sock, [inst], limit=10 ** 6)
        self.assertGreater(r["exact"], 0)
        self.assertNotIn(12000, [s["seed"] for s in r["seeds"]])           # never the jewel's own seed
        own = td.read(2, 12000, inst)
        for s in r["seeds"][:20]:                                         # Lethal Pride: exactly the same addition
            self.assertEqual(td.read(2, s["seed"], inst), own)
        g = tj.equivalents(1, 2000, sock, [inst], limit=10 ** 6)          # Glorious Vanity: same passive, rolls >=
        want = td.read(1, 2000, inst)
        for s in g["seeds"][:20]:
            got = td.read(1, s["seed"], inst)
            self.assertEqual(got[0], want[0])
            self.assertTrue(all(a >= b for a, b in zip(got[1:], want[1:])))
        e = tj.equivalents(5, 116820, sock, [inst], limit=50)            # Elegant Hubris: seeds are multiples of 20
        self.assertTrue(all(s["seed"] % 20 == 0 for s in e["seeds"]))
        everything = tj.equivalents(2, 12000, sock)                       # all notables: the closest ones
        self.assertEqual(len(everything["core"]), sum(1 for n in tj.sockets() if n["id"] == sock for _ in n["notables"]))
        self.assertTrue(everything["seeds"])

    def test_equivalent_seeds_are_priced_in_one_search(self):
        from unittest import mock
        from poecraft.data import trade
        from poecraft.engine import evaluator, timeless as tj
        from poecraft.messages import render
        sock = next(s["id"] for s in tj.sockets() if "Instability" in s["notables"])
        inst = self.ids["Instability"]
        seeds = [s["seed"] for s in tj.equivalents(2, 12000, sock, [inst], limit=10 ** 6)["seeds"]]
        searches = []

        def search(lg, payload, wait=20.0):
            searches.append(payload)
            return {"id": f"q{len(searches)}", "total": 2, "result": ["a", "b"]}

        def fetch(q, ids, wait=20.0):                                     # two listed equivalents, the dearer first
            return [{"listing": {"price": {"amount": a, "currency": "chaos"}, "account": {"online": {"league": "L"}}},
                     "item": {"name": "Lethal Pride", "typeLine": "Timeless Jewel",
                              "explicitMods": [f"Commanded leadership over {s} warriors under Kaom",
                                               "Passives in radius are Conquered by the Karui"]}}
                    for a, s in ((40, seeds[1]), (5, seeds[0]))]
        text = "Lethal Pride\nTimeless Jewel\n--------\nCommanded leadership over 12000 warriors under Kaom\n"
        evaluator._CACHE.clear()
        trade._reset_limits()
        with mock.patch.object(trade, "search_ids", search), mock.patch.object(trade, "fetch_listings", fetch), \
                mock.patch.object(trade, "leagues", lambda realm="pc": ["Allflame"]), \
                mock.patch("poecraft.engine.evaluator._price_league", return_value="Allflame"):
            r = render(evaluator.evaluate(text, socket=sock, equivalents=[inst]), "ru")
        eq = r["timeless"]["equivalents"]
        scan = [p for p in searches if p["query"]["stats"][0]["type"] == "and"][-1]["query"]["stats"][0]["filters"][0]
        self.assertEqual(scan["value"], {"min": 10000, "max": 18000})    # the market scan: every seed of the jewel
        group = searches[-1]["query"]["stats"][0]
        self.assertEqual(group["type"], "count")
        self.assertLessEqual(len(group["filters"]), tj.MAX_EQUIVALENTS)  # the trade site refuses more than ~35
        self.assertEqual(group["filters"][0]["id"], "explicit.pseudo_timeless_jewel_kaom")
        self.assertEqual([(s["seed"], s["listing"]["price"]["amount"]) for s in eq["seeds"][:2]],
                         [(seeds[0], 5), (seeds[1], 40)])                 # cheapest first, seed read from the listing
        self.assertEqual(eq["listed"], 2)
        self.assertTrue(eq["seeds"][0]["url"].startswith("https://www.pathofexile.com/trade/search/Allflame?q="))
        self.assertFalse(eq["closest"])

    def test_russian_paste_and_evaluator_calculation(self):
        from unittest import mock
        from poecraft.data import trade
        from poecraft.engine import evaluator, timeless as tj
        from poecraft.messages import render
        info = tj.parse("Класс предмета: Самоцветы\nРедкость: Уникальный\nТщеславие\nВневременной самоцвет\n--------\n"
                        "Омыт в крови 2000 жертв во имя Шибаквы\nПассивные умения в радиусе завоеваны ваал\n")
        self.assertEqual((info["version"], info["seed"], info["conqueror"], info["lang"]), (1, 2000, "Xibaqua", "ru"))
        inst = self.ids["Instability"]
        sock = next(s["id"] for s in tj.sockets() if "Instability" in s["notables"])
        evaluator._CACHE.clear()
        with mock.patch.object(trade, "search_ids", lambda lg, p, wait=20.0: {"id": "q", "total": 0, "result": []}), \
                mock.patch("poecraft.engine.evaluator._price_league", return_value="Allflame"), \
                mock.patch.object(trade, "leagues", lambda realm="pc": ["Allflame"]):
            text = "Glorious Vanity\nTimeless Jewel\n--------\nBathed in the blood of 2000 sacrificed in the name of Xibaqua\n"
            r = render(evaluator.evaluate(text, socket=sock), "ru")
            again = render(evaluator.evaluate(text, socket=sock), "en")          # cached trade part, fresh calculation
        node = next(e for e in r["timeless"]["calc"]["nodes"] if e["id"] == inst)
        self.assertEqual(node["new_name"], "[EN] Thaumaturgical Protection")    # English tree text, marked
        self.assertTrue(again.get("cached"))
        self.assertEqual(len(again["timeless"]["calc"]["nodes"]), len(r["timeless"]["calc"]["nodes"]))


class TradeRequestBudget(unittest.TestCase):
    """Fewer requests to GGG: the per-minute cap, and stricter searches answered from one that found nothing."""

    def setUp(self):
        from poecraft.data import trade
        trade._reset_limits()

    def test_per_minute_cap_is_below_gggs_rule(self):
        from poecraft import config as cfg
        from poecraft.data import trade
        caps = {period: cap() for cap, period in trade._CAPS["search"]}
        self.assertLess(caps[60], 15)                                    # GGG: 15 searches / 60 s per IP
        self.assertLess(caps[10], 5)
        self.assertLess(caps[300], 30)
        self.assertEqual(caps[60], cfg.TRADE_MAX_SEARCH_PER_MIN)

    def test_a_stricter_search_than_an_empty_one_is_not_sent(self):
        import json
        from unittest import mock
        from poecraft.data import trade
        sent = []

        def fake_ggg(kind, req, wait):
            sent.append(json.loads(req.data))
            return {"id": f"q{len(sent)}", "total": 0, "result": []}
        base = lambda stats, ilvl=None: {"query": dict({"status": {"option": "online"}, "type": "Crusader Boots",
                                                        "stats": [{"type": "and", "filters": stats}]},
                                                       **({"filters": {"misc_filters": {"filters": {"ilvl": {"min": ilvl}}}}}
                                                          if ilvl else {})), "sort": {"price": "asc"}}
        life = {"id": "s.life", "value": {"min": 70}}
        with mock.patch.object(trade, "_ggg", fake_ggg):
            trade.search_ids("L", base([life]))                                          # sent: found nothing
            r = trade.search_ids("L", base([{"id": "s.life", "value": {"min": 80}}, {"id": "s.ms"}], ilvl=86))
            self.assertEqual((r["total"], r.get("inferred")), (0, True))                # stricter: not sent
            trade.search_ids("L", base([{"id": "s.life", "value": {"min": 60}}]))       # looser: must be sent
            trade.search_ids("Other", base([life, {"id": "s.ms"}]))                     # other league: sent
            trade.search_ids("L", dict(base([life, {"id": "s.ms"}]), query=dict(base([life])["query"],
                                                                                  status={"option": "any"})))
        self.assertEqual(len(sent), 4)

    def test_evaluation_waits_for_slots_within_its_budget(self):
        import time
        from unittest import mock
        from poecraft.engine import evaluator
        out = {}
        with mock.patch.object(evaluator.config, "EVAL_WAIT_BUDGET", 30):
            first = evaluator._wait(out)
            self.assertTrue(29 <= first <= 30)
            out["_deadline"] = time.monotonic() - 1                      # budget used up: no more waiting
            self.assertEqual(evaluator._wait(out), 0.0)


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class AuditPlannerAndResilience(unittest.TestCase):
    """Planner import edge cases, trade links without stat ids, one shared trade limiter, bounded caches, errors."""

    def setUp(self):
        from poecraft.data import trade
        trade._reset_limits()

    def test_fractured_and_influenced_item_imported_as_have(self):
        from poecraft.data.repository import get_data
        from poecraft.engine.itemtext import parse_item_text
        from poecraft.engine.planner import Planner
        from poecraft.messages import render
        p = parse_item_text("Item Class: Helmets\nRarity: Rare\nGrim Visage\nHubris Circlet\n--------\nItem Level: 86\n"
                            "--------\n+80 to maximum Life (fractured)\n+40% to Cold Resistance\n--------\nShaper Item\n",
                            get_data())
        self.assertEqual(p["influences"], ["shaper"])
        fractured = [t for t in p["targets"] if t.get("fractured")]
        self.assertEqual(len(fractured), 1)
        self.assertEqual(len(p["have"]), 1)                              # the cold resistance roll
        r = Planner(sims=0).recover(p["base_id"], p["ilvl"], p["influences"], p["targets"], [h["id"] for h in p["have"]],
                                    lang=None)
        self.assertIn("steps", json.dumps(render(r, "ru"), ensure_ascii=False))

    def test_white_base_implicit_is_not_imported_as_a_target(self):
        from poecraft.data.repository import get_data
        from poecraft.engine.itemtext import parse_item_text
        p = parse_item_text("Item Class: Body Armours\nRarity: Normal\nAstral Plate\n--------\nItem Level: 84\n--------\n"
                            "+12% to all Elemental Resistances (implicit)\n", get_data())
        self.assertEqual(p["targets"], [])                               # was: an Eater of Worlds implicit
        self.assertEqual(p["lines"][0]["matched"], False)

    def test_planner_import_of_things_it_cannot_craft_says_why(self):
        from poecraft.data.repository import get_data
        from poecraft.engine.itemtext import parse_item_text
        from poecraft.messages import render
        cases = {"Item Class: Divination Cards\nRarity: Divination Card\nThe Doctor\n": "divination card",
                 "Item Class: Stackable Currency\nRarity: Currency\nDivine Orb\n": "currency",
                 "Item Class: Jewels\nRarity: Unique\nWatcher's Eye\nPrismatic Jewel\n": "unique's base"}
        for text, word in cases.items():
            p = parse_item_text(text, get_data())
            self.assertEqual(p["targets"], [])
            self.assertIn(word, render(p["warnings"][0], "en"))
        p = parse_item_text("Item Class: Boots\nRarity: Rare\nX\nCrusader Boots\n--------\nItem Level: 86\n--------\n"
                            "+82 to maximum Life\n--------\nCorrupted\n", get_data())
        self.assertEqual(p["status"], ["corrupted"])
        self.assertTrue(any(render(w, "ru").startswith("Осквернённый предмет") for w in p["warnings"]))

    def test_trade_link_with_no_mappable_stats(self):
        from unittest import mock
        from poecraft.data import trade
        from poecraft.engine.tradelink import trade_search
        from poecraft.messages import render
        with mock.patch.object(trade, "stat_index", lambda: {"explicit": {}, "implicit": {}, "fractured": {},
                                                             "crafted": {}, "_alts": {}}), \
                mock.patch.object(trade, "short_url", side_effect=AssertionError("must not be asked")) as short, \
                mock.patch.object(trade, "leagues", lambda realm="pc": ["Allflame"]), \
                mock.patch("poecraft.engine.tradelink._price_league", return_value="Allflame"):
            from poecraft.data.repository import get_data
            base = get_data().base_by_name("Crusader Boots")
            r = render(trade_search(base.id, 86, [], [{"key": "prefix|base_maximum_life", "min": 70},
                                                      {"key": "prefix|no_such_mod", "label": "Imaginary", "min": 1}],
                                    shorten=False), "en")
        self.assertTrue(r["url"].startswith("https://www.pathofexile.com/trade/search/Allflame"))
        self.assertFalse(short.called)
        self.assertTrue(r["warnings"])

    def test_find_on_trade_and_evaluator_share_one_limiter(self):
        from unittest import mock
        from poecraft import config as cfg
        from poecraft.data import trade
        with mock.patch.object(trade, "_post_search", lambda league, payload: "https://x/short"), \
                mock.patch.object(trade, "SHORT_URL_WAIT", 0.0):
            for i in range(cfg.TRADE_MAX_PER_10S):                      # evaluator searches use up the 10 s budget
                trade._slot("search", 0.0)                              # (the slot each search_ids request takes)
            with self.assertRaises(trade.TradeError) as cm:            # "Find on Trade" sees the same limit
                trade.short_url("L", {"query": {"type": "other"}})
        from poecraft.server import _error
        self.assertIn("следующий поиск освободится", _error(cm.exception, "ru"))

    def test_caches_are_bounded(self):
        import time
        from poecraft.data import trade
        from poecraft.engine import evaluator
        cache = {}
        for i in range(trade.CACHE_MAX + 50):
            with trade._lock:
                trade._bounded(cache, time.monotonic())
                cache[i] = (time.monotonic() + 300, i)
        self.assertLessEqual(len(cache), trade.CACHE_MAX)
        self.assertGreaterEqual(evaluator.CACHE_MAX, 100)

    def test_failed_data_download_is_not_retried_by_every_request(self):
        import urllib.error
        from unittest import mock
        from poecraft.data import trade
        calls = []

        def down(*a, **k):
            calls.append(1)
            raise urllib.error.URLError("down")
        with mock.patch("urllib.request.urlopen", down), \
                mock.patch.object(trade, "_cache", lambda name: config.DATA_DIR / "no_such_dir" / f"{name}.json"):
            for _ in range(3):
                with self.assertRaises(trade.TradeError):
                    trade.leagues()
        self.assertEqual(len(calls), 1)

    def test_unexpected_server_error_is_a_localized_json_answer(self):
        import http.client
        import threading
        from unittest import mock
        from poecraft import server
        from poecraft.engine import evaluator
        httpd = server.BoundedServer(("127.0.0.1", 0), server.Handler, 4)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(evaluator, "evaluate", side_effect=RuntimeError("boom")), \
                    mock.patch("traceback.print_exc"):
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
                conn.request("POST", "/api/evaluate", json.dumps({"text": "x", "lang": "ru"}), {"Content-Type": "application/json"})
                resp = conn.getresponse()
                body = json.loads(resp.read())
                conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
        self.assertEqual(resp.status, 500)
        self.assertTrue(body["error"].startswith("На сервере произошла ошибка"))
        self.assertNotIn("boom", body["error"])                          # no internals for the visitor


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class UserFlows(unittest.TestCase):
    """The two user journeys over HTTP, in every language: Hub -> Planner -> import -> plan -> trade link, and
    Hub -> Evaluator -> paste -> price. The trade site is faked; everything else is the real server."""
    ITEM = ("Item Class: Boots\nRarity: Rare\nDoom Stride\nCrusader Boots\n--------\nItem Level: 86\n--------\n"
            "+82 to maximum Life\n25% increased Movement Speed\n+40% to Fire Resistance\n")

    @classmethod
    def setUpClass(cls):
        import threading
        from unittest import mock
        from poecraft import server
        from poecraft.data import trade
        cls.patches = [
            mock.patch.object(trade, "search_ids", lambda lg, p, wait=20.0: {"id": "q", "total": 2, "result": ["a", "b"]}),
            mock.patch.object(trade, "fetch_listings", lambda q, ids, wait=20.0: [
                {"listing": {"price": {"amount": a, "currency": "chaos"}, "account": {"online": {"league": "L"}}},
                 "item": {"typeLine": "Crusader Boots", "ilvl": 86, "explicitMods": []}} for a in (60, 45)]),
            mock.patch.object(trade, "short_url", lambda league, payload: "https://www.pathofexile.com/trade/search/Allflame/abc"),
            mock.patch.object(trade, "leagues", lambda realm="pc": ["Allflame", "Standard"]),
            mock.patch("poecraft.engine.evaluator._price_league", return_value="Allflame"),
            mock.patch("poecraft.engine.tradelink._price_league", return_value="Allflame"),
        ]
        for p in cls.patches:
            p.start()
        cls.httpd = server.BoundedServer(("127.0.0.1", 0), server.Handler, 8)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in cls.patches:
            p.stop()

    def req(self, method, path, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=300)
        conn.request(method, path, json.dumps(body) if body is not None else None, {"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, raw

    def test_hub_planner_import_plan_trade_link(self):
        st, hub = self.req("GET", "/")
        st_js, js = self.req("GET", "/hub.js")
        self.assertEqual((st, st_js), (200, 200))
        self.assertIn(b'href: "/craft"', js)
        self.assertEqual(self.req("GET", "/craft")[0], 200)
        for lang in ("en", "ru", "de"):
            st, raw = self.req("POST", "/api/import", {"text": self.ITEM, "lang": lang})
            imp = json.loads(raw)
            self.assertEqual((st, imp["base_name"]), (200, "Crusader Boots"))
            self.assertEqual(len(imp["targets"]), 3)
            st, raw = self.req("POST", "/api/plan", {"base": imp["base_id"], "ilvl": imp["ilvl"], "influences": [],
                                                     "targets": imp["targets"][:2], "sims": 0, "lang": lang})
            plan = json.loads(raw)
            self.assertEqual(st, 200, plan)
            self.assertTrue(plan.get("steps") or plan.get("comparison"))
            st, raw = self.req("POST", "/api/trade", {"base": imp["base_id"], "ilvl": imp["ilvl"], "influences": [],
                                                      "targets": imp["targets"], "lang": lang})
            link = json.loads(raw)
            self.assertEqual((st, link["url"]), (200, "https://www.pathofexile.com/trade/search/Allflame/abc"))

    def test_hub_evaluator_paste_price(self):
        st, js = self.req("GET", "/hub.js")
        self.assertIn(b'href: "/evaluator"', js)
        st, page = self.req("GET", "/evaluator")
        self.assertEqual(st, 200)
        notes = {}
        from poecraft.engine import evaluator
        for lang in ("en", "ru", "de"):
            evaluator._CACHE.clear()
            st, raw = self.req("POST", "/api/evaluate", {"text": self.ITEM, "lang": lang})
            r = json.loads(raw)
            self.assertEqual((st, r["kind"]), (200, "rare"))
            self.assertEqual((r["summary"]["price"]["amount"], r["summary"]["online"]), (45, 2))   # cheapest first
            self.assertEqual([l["price"]["amount"] for l in r["listings"]], [45, 60])
            notes[lang] = r["ranking"][0]["why"]
        self.assertEqual(len(set(notes.values())), 3)                   # explained in each language
        self.assertTrue(any(ch in notes["ru"] for ch in "абвгдежзиклмнопрстуфхцчшщыэюя"))


class TradeStatMatching(unittest.TestCase):
    """Mod lines vs the trade site's stat texts: units are plural-blind, duplicated texts become 'any of'."""
    INDEX = {"implicit": {"Ignites you inflict spread to other Enemies within # metre": "implicit.stat_2011785027",
                          "#% of Fire Damage Leeched as Life": "implicit.stat_3848282610"},
             "explicit": {"+# to maximum Life": "explicit.stat_3299347043"}, "fractured": {}, "crafted": {},
             "_alts": {"implicit": {"#% of Fire Damage Leeched as Life": ["implicit.stat_3848282610",
                                                                          "implicit.stat_1743742391"]}}}

    def test_plural_blind_units(self):
        from poecraft.data import trade
        self.assertEqual(trade.trade_stat(self.INDEX, "implicit", "Ignites you inflict spread to other Enemies within # metres",
                                          False), "implicit.stat_2011785027")
        self.assertIsNone(trade.trade_stat(self.INDEX, "implicit", "Something else entirely", False))

    def test_duplicate_texts_become_an_any_of_group(self):
        from poecraft.data import trade
        ids = trade.trade_stat_ids(self.INDEX, "implicit", "#% of Fire Damage Leeched as Life", False)
        self.assertEqual(ids, ["implicit.stat_3848282610", "implicit.stat_1743742391"])
        groups = trade.stats_groups([{"id": "explicit.stat_3299347043", "value": {"min": 70}},
                                     {"id": ids[0], "alts": ids, "value": {"min": 0.18}}])
        self.assertEqual(groups[0], {"type": "and", "filters": [{"id": "explicit.stat_3299347043", "value": {"min": 70}}]})
        self.assertEqual(groups[1]["type"], "count")
        self.assertEqual(groups[1]["value"], {"min": 1})
        self.assertEqual([f["id"] for f in groups[1]["filters"]], ids)
        self.assertTrue(all(f["value"] == {"min": 0.18} for f in groups[1]["filters"]))


class RateLimiterUnit(unittest.TestCase):
    def test_token_bucket(self):
        from poecraft.ratelimit import RateLimiter
        rl = RateLimiter(rate=10, burst=20)
        results = [rl.allow("a", now=100.0)[0] for _ in range(25)]
        self.assertEqual(results.count(True), 20)                 # the burst
        ok, wait = rl.allow("a", now=100.0)
        self.assertFalse(ok)
        self.assertAlmostEqual(wait, 0.1, places=6)               # 10 per second refill
        self.assertTrue(rl.allow("a", now=100.2)[0])               # refilled after 0.1 s (float margin)
        self.assertTrue(rl.allow("b", now=100.0)[0])               # another client has its own bucket

    def test_memory_stays_bounded(self):
        from poecraft.ratelimit import RateLimiter
        rl = RateLimiter(rate=1, burst=1, max_keys=100)
        for i in range(500):
            rl.allow(i, now=float(i * 10))                       # each key idle long enough to be swept
        self.assertLessEqual(len(rl._buckets), 101)


class TradeSearchCache(unittest.TestCase):
    """Many users clicking 'Find on Trade': identical searches are cached, the trade site is asked a capped rate."""

    def setUp(self):
        from poecraft.data import trade
        trade._reset_limits()
        self.addCleanup(trade._reset_limits)

    def test_identical_searches_hit_the_trade_site_once(self):
        from unittest import mock
        from poecraft.data import trade
        payload = {"query": {"type": "Crusader Boots"}}
        with mock.patch.object(trade, "_post_search", return_value="https://x/trade/search/L/abc") as post:
            urls = [trade.short_url("Allflame", payload) for _ in range(100)]
        self.assertEqual(post.call_count, 1)
        self.assertEqual(set(urls), {"https://x/trade/search/L/abc"})

    def test_rate_cap_and_429_fall_back_without_calling(self):
        from unittest import mock
        from poecraft import config as cfg
        from poecraft.data import trade
        with mock.patch.object(trade, "_post_search", side_effect=lambda lg, p: f"u{len(str(p))}") as post, \
                mock.patch.object(cfg, "TRADE_MAX_PER_10S", 3):
            for i in range(3):
                trade.short_url("Allflame", {"q": i})
            with self.assertRaises(trade.TradeError):
                trade.short_url("Allflame", {"q": 99})             # 4th distinct search within 10 s: not sent
            self.assertEqual(post.call_count, 3)
        trade._reset_limits()
        err = trade.TradeError("HTTP 429")
        err.retry_after = 60.0
        with mock.patch.object(trade, "_post_search", side_effect=err) as post:
            with self.assertRaises(trade.TradeError):
                trade.short_url("Allflame", {"q": 1})
            with self.assertRaises(trade.TradeError):
                trade.short_url("Allflame", {"q": 2})              # blocked: the site asked to wait
            self.assertEqual(post.call_count, 1)

    def test_user_agent_everywhere(self):
        import inspect
        from poecraft import config as cfg, ocr
        from poecraft.data import fetch, locales, ninja, trade
        for mod in (fetch, locales, ocr, trade, ninja):
            src = inspect.getsource(mod)
            self.assertNotIn('"poecraft-planner/1.0"', src, mod.__name__)
        from unittest import mock
        with mock.patch.object(cfg, "CONTACT", "ops@example.org"), mock.patch.object(cfg, "USER_AGENT_OVERRIDE", ""):
            self.assertIn("contact: ops@example.org", cfg.user_agent())
            self.assertEqual(ninja.user_agent(), cfg.user_agent())


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class ProductionServer(unittest.TestCase):
    """The server in production mode: health, headers, limits - started for real on a free port."""

    @classmethod
    def setUpClass(cls):
        import threading
        from poecraft import server
        from poecraft.ratelimit import RateLimiter
        cls.server_mod = server
        cls.saved = dict(server._LIMITS), server.Handler.production
        server.Handler.production = True
        server._LIMITS["api"] = RateLimiter(0.01, 3)              # tiny limit so the test can hit it
        cls.httpd = server.BoundedServer(("127.0.0.1", 0), server.Handler, 8)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_mod._LIMITS.clear()
        cls.server_mod._LIMITS.update(cls.saved[0])
        cls.server_mod.Handler.production = cls.saved[1]

    def req(self, path, data=None, headers=None, method=None):
        import urllib.error
        import urllib.request
        r = urllib.request.Request(self.base + path, data, headers or {}, method=method)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def test_health_headers_and_rate_limit(self):
        code, headers, body = self.req("/api/health")
        self.assertEqual(code, 200)
        self.assertIn(b'"production": true', body)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertNotIn("Python", headers.get("Server", ""))
        codes = [self.req("/api/locales", headers={"X-Forwarded-For": "203.0.113.9"})[0] for _ in range(6)]
        self.assertIn(429, codes)                                  # burst of 3, then limited
        code, headers, _ = self.req("/api/locales")
        self.assertIn(code, (200, 429))

    def test_rate_limit_answer_is_in_the_visitors_language(self):
        def limited(headers):
            for _ in range(8):
                code, _, body = self.req("/api/locales", headers=headers)
                if code == 429:
                    return json.loads(body)["error"]
            self.fail("no 429")
        self.assertTrue(limited({"X-Forwarded-For": "203.0.113.21", "X-Poecraft-Lang": "ru"}).startswith("Слишком много"))
        self.assertTrue(limited({"X-Forwarded-For": "203.0.113.22", "Accept-Language": "de-DE,de;q=0.9"}).startswith("Zu viele"))
        self.assertTrue(limited({"X-Forwarded-For": "203.0.113.23"}).startswith("Too many"))

    def test_body_limit_and_cors(self):
        from unittest import mock
        from poecraft import config as cfg
        with mock.patch.object(cfg, "MAX_BODY", 100):
            code, _, _ = self.req("/api/import", data=b"{" + b" " * 500 + b"}", headers={"Content-Type": "application/json"})
        self.assertEqual(code, 413)
        with mock.patch.object(cfg, "CORS_ORIGINS", ["https://front.example.org"]):
            code, headers, _ = self.req("/api/health", method="OPTIONS", headers={"Origin": "https://front.example.org"})
            self.assertEqual(code, 204)
            self.assertEqual(headers["Access-Control-Allow-Origin"], "https://front.example.org")
            code, _, _ = self.req("/api/health", method="OPTIONS", headers={"Origin": "https://evil.example"})
            self.assertEqual(code, 403)

    def test_busy_planner_answers_503(self):
        import json
        from unittest import mock
        from poecraft import config as cfg
        server = self.server_mod
        server._plan_lock.acquire()
        try:
            with mock.patch.object(cfg, "PLAN_QUEUE_WAIT", 0):
                code, _, body = self.req("/api/plan", data=json.dumps({"base": "x", "targets": []}).encode(),
                                         headers={"Content-Type": "application/json"})
        finally:
            server._plan_lock.release()
        self.assertEqual(code, 503)
        self.assertIn("busy", json.loads(body)["error"])


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class TradeLinksAndTemplates(unittest.TestCase):
    """'Find on Trade' (offline: the trade site is mocked) and CLI templates."""
    STATS = {"explicit": {"+# to maximum Life": "explicit.stat_3299347043",
                          "#% increased Movement Speed": "explicit.stat_2250533757",
                          "+#% to Fire Resistance": "explicit.stat_3372524247",
                          "+# to Armour (Local)": "explicit.stat_3484657501",
                          "#% increased Armour and Energy Shield (Local)": "explicit.stat_3321629045"},
             "fractured": {"+#% to Fire Resistance": "fractured.stat_3372524247"}, "implicit": {}, "crafted": {}}

    def search(self, targets, leagues=("Allflame", "Hardcore Allflame", "Standard"), price_league="Allflame",
               short=None, league=None):
        from unittest import mock
        from poecraft.data import trade
        from poecraft.data.repository import get_data
        from poecraft.engine import tradelink
        from poecraft.messages import render
        base = get_data().base_by_name("Crusader Boots").id

        def leagues_fn(realm="pc"):
            if leagues is None:
                raise trade.TradeError("offline")
            return list(leagues)

        def short_fn(lg, payload):
            if short is None:
                raise trade.TradeError("HTTP 429")
            return f"{trade.SITE}/trade/search/{lg}/{short}"
        with mock.patch.object(trade, "stat_index", lambda: self.STATS), \
                mock.patch.object(trade, "leagues", leagues_fn), mock.patch.object(trade, "short_url", short_fn), \
                mock.patch.object(tradelink, "_price_league", lambda: price_league):
            return render(tradelink.trade_search(base, 86, [], targets, league=league), "en")

    def test_query_maps_mods_and_uses_the_current_league(self):
        r = self.search([{"signature": "prefix|base_maximum_life", "min": 70},
                         {"signature": "prefix|base_movement_velocity_+%", "min": 25},
                         {"signature": "prefix|local_armour_and_energy_shield_+%", "min": 80},
                         {"signature": "suffix|base_fire_damage_resistance_%", "min": 36, "fractured": True}],
                        short="H4sIAAAA-x_y")
        self.assertEqual(r["league"], "Allflame")
        self.assertEqual(r["league_source"], "prices")
        self.assertEqual(r["url"], "https://www.pathofexile.com/trade/search/Allflame/H4sIAAAA-x_y")
        q = r["payload"]["query"]
        self.assertEqual(q["type"], "Crusader Boots")
        self.assertEqual([(f["id"], f.get("value", {}).get("min")) for f in q["stats"][0]["filters"]],
                         [("explicit.stat_3299347043", 70), ("explicit.stat_2250533757", 25),
                          ("explicit.stat_3321629045", 80), ("fractured.stat_3372524247", 36)])
        self.assertEqual(r["warnings"], [])

    def test_hybrid_one_filter_per_stat_and_unmapped_lines_warn(self):
        r = self.search([{"signature": "prefix|base_maximum_life", "min": 70},
                         {"signature": "prefix|base_maximum_life,local_base_physical_damage_reduction_rating", "min": 20},
                         {"signature": "suffix|base_cold_damage_resistance_%", "min": 36}], short="abc")
        ids = [f["id"] for f in r["payload"]["query"]["stats"][0]["filters"]]
        self.assertEqual(ids.count("explicit.stat_3299347043"), 1)        # Life once, with the larger minimum
        self.assertIn("explicit.stat_3484657501", ids)                     # the hybrid's Armour line (local)
        self.assertTrue(any("No trade-site stat for '+#% to Cold Resistance'" in w for w in r["warnings"]))

    def test_league_fallbacks(self):
        r = self.search([{"signature": "prefix|base_maximum_life", "min": 70}], price_league=None, short="abc")
        self.assertEqual((r["league"], r["league_source"]), ("Allflame", "trade"))   # first league the site lists
        r = self.search([{"signature": "prefix|base_maximum_life"}], leagues=None, price_league=None, short="abc")
        self.assertEqual((r["league"], r["league_source"]), ("Standard", "fallback"))
        self.assertTrue(any(w.startswith("The current league could not be determined") for w in r["warnings"]))
        r = self.search([{"signature": "prefix|base_maximum_life"}], short="abc", league="Hardcore Allflame")
        self.assertEqual(r["url"], "https://www.pathofexile.com/trade/search/Hardcore Allflame/abc")

    def test_short_link_failure_falls_back_to_the_full_query(self):
        import json
        import urllib.parse
        r = self.search([{"signature": "prefix|base_maximum_life", "min": 70}], short=None)
        self.assertFalse(r["short"])
        self.assertTrue(r["url"].startswith("https://www.pathofexile.com/trade/search/Allflame?q="))
        q = json.loads(urllib.parse.unquote(r["url"].split("?q=", 1)[1]))
        self.assertEqual(q["query"]["stats"][0]["filters"][0], {"id": "explicit.stat_3299347043", "value": {"min": 70}})
        self.assertTrue(any("did not return a short link" in w for w in r["warnings"]))

    def test_cli_templates_save_and_load(self):
        import io
        import os
        import tempfile
        from contextlib import redirect_stderr, redirect_stdout
        from unittest import mock
        from poecraft import templates
        from poecraft.__main__ import main
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"POECRAFT_TEMPLATES": os.path.join(tmp, "t.json")}):
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                main(["plan", "--base", "Crusader Boots", "--ilvl", "84", "--target", "+# to maximum Life>=70",
                      "--target", "Fire Resistance>=36", "--sims", "0", "--no-live-prices", "--quiet",
                      "--save-template", "Endgame Boots"])
            self.assertIn("Template 'Endgame Boots' saved", out.getvalue())
            t = templates.get("Endgame Boots")
            self.assertEqual((t["base"], t["ilvl"], len(t["targets"])), ("Crusader Boots", 84, 2))
            self.assertEqual(t["targets"][0]["min"], 70)
            out2 = io.StringIO()
            with redirect_stdout(out2), redirect_stderr(io.StringIO()):
                main(["plan", "--load-template", "Endgame Boots", "--sims", "0", "--no-live-prices", "--quiet"])
            text = out2.getvalue()
            self.assertIn("Crusader Boots (Boots), ilvl 84", text)
            self.assertIn("+# to maximum Life (>= 70)", text)
            out3 = io.StringIO()
            with redirect_stdout(out3):
                main(["templates"])
            self.assertIn("Endgame Boots", out3.getvalue())


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class EventOnlyMods(unittest.TestCase):
    """Battle Royale mods carry normal spawn weights in the export but never roll in standard crafting."""

    def test_royale_mods_are_in_no_pool(self):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.context import ItemContext
        from poecraft.engine.modsearch import _class_index
        data, rules = get_data(), get_rules()
        royale = [m for m in data.mods.values() if "Royale" in m.id and m.domain == "item"
                  and any(w > 0 for _, w in m.spawn_weights)]
        self.assertEqual(len(royale), 4)                    # the export really gives these normal spawn weights
        for name in ("Crusader Boots", "Two-Toned Boots", "Sorcerer Gloves", "Imbued Wand", "Corsair Sword"):
            base = data.base_by_name(name)
            ctx = ItemContext(data, rules, base, 86, ())
            pool = ctx.pool()                               # what the analytic odds, simulation and recovery use
            self.assertFalse([e.mod.id for e in ctx.base_entries if "Royale" in e.mod.id], name)
            self.assertFalse([e.mod.id for e in pool if "Royale" in e.mod.id], name)
            self.assertFalse([t["mod_id"] for f in ctx.families().values() for t in f.tiers if "Royale" in t["mod_id"]])
            self.assertFalse([e["mod"].id for e in _class_index(data, base.item_class).values() if "Royale" in e["mod"].id])
        boots = ItemContext(data, rules, data.base_by_name("Crusader Boots"), 86, ())
        ms = boots.families()["prefix|base_movement_velocity_+%"]
        self.assertEqual(sorted(t["min"] for t in ms.tiers if t["source"] == "roll"), [10, 15, 20, 25, 30, 35])   # no 15-25 Royale tier


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class ItemTextImport(unittest.TestCase):
    """Pasted item text (Ctrl+C / Ctrl+Alt+C) -> base, item level, influences, targets and 'have' mod ids."""
    EN = """Item Class: Boots
Rarity: Rare
Doom Stride
Crusader Boots
--------
Armour: 139
Energy Shield: 28
--------
Requirements:
Level: 64
--------
Sockets: B-B-R-R
--------
Item Level: 86
--------
+30% to Cold Resistance (implicit)
--------
+82 to maximum Life
25% increased Movement Speed
+40% to Fire Resistance
+38% to Lightning Resistance
+50 to maximum Mana (crafted)
Grants Immunity to Bleeding for 4 seconds if used while Bleeding
--------
Crusader Item
"""

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data
        cls.data = get_data()

    def parse(self, text):
        from poecraft.engine.itemtext import parse_item_text
        from poecraft.messages import render
        return render(parse_item_text(text), "en")

    def test_english_text(self):
        r = self.parse(self.EN)
        self.assertEqual((r["base_name"], r["item_class"], r["ilvl"], r["rarity"], r["influences"]),
                         ("Crusader Boots", "Boots", 86, "rare", ["crusader"]))
        keys = [t["key"] for t in r["targets"]]
        self.assertEqual(keys, ["prefix|base_maximum_life", "prefix|base_movement_velocity_+%",
                                "suffix|base_fire_damage_resistance_%", "suffix|base_lightning_damage_resistance_%",
                                "prefix|base_maximum_mana"])
        life = r["targets"][0]
        self.assertEqual(life["value"], 82)
        self.assertLessEqual(life["min"], 82)                         # the tier's lower bound
        self.assertIn("IncreasedLife5", [h["id"] for h in r["have"]])
        self.assertNotIn("+30% to Cold Resistance", " ".join(t["label"] for t in r["targets"]))   # base implicit
        self.assertTrue(any("Grants Immunity to Bleeding" in w and "skipped" in w for w in r["warnings"]))
        self.assertTrue(any("Crafted modifier imported as a target" in w for w in r["warnings"]))

    def test_russian_text_from_the_games_own_strings(self):
        import re
        from poecraft import i18n
        def ru(mid, *vals):
            t = i18n.game_text("mod", mid, self.data.mods[mid].text, "ru")[0]
            it = iter(vals)
            return re.sub(r"\(-?[\d.]+--?[\d.]+\)", lambda m: str(next(it)), t)
        b = self.data.base_by_name("Crusader Boots")
        name_ru, ok = i18n.game_text("base", b.id, b.name, "ru")
        self.assertTrue(ok)
        text = (f"Класс предмета: Ботинки\nРедкость: Редкий\nГибель путника\n{name_ru}\n--------\nБроня: 139\n"
                f"Энергетический щит: 28\n--------\nУровень предмета: 84\n--------\n{ru('IncreasedLife5', 82)}\n"
                f"{ru('FireResist6', 40)}\n{ru('LightningResist6', 38)}\n")
        r = self.parse(text)
        self.assertEqual((r["base_name"], r["ilvl"], r["rarity"]), ("Crusader Boots", 84, "rare"))
        self.assertEqual([h["id"] for h in r["have"]], ["IncreasedLife5", "FireResist6", "LightningResist6"])
        self.assertEqual(r["warnings"], [])

    def test_advanced_copy_hybrid_and_fractured(self):
        text = """Item Class: Boots
Rarity: Rare
Doom Stride
Crusader Boots
--------
Item Level: 84
--------
{ Prefix Modifier "Rhino's" (Tier: 4) — Defences, Life }
+25(20-32) to Armour
+20(18-23) to maximum Life
{ Suffix Modifier "of the Volcano" (Tier: 3) — Elemental, Fire, Resistance }
+40(36-41)% to Fire Resistance (fractured)
--------
Fractured Item
"""
        r = self.parse(text)
        self.assertEqual([(t["key"], t.get("fractured", False)) for t in r["targets"]],
                         [("prefix|base_maximum_life,local_base_physical_damage_reduction_rating", False),
                          ("suffix|base_fire_damage_resistance_%", True)])
        self.assertEqual([h["id"] for h in r["have"]], ["LocalBaseArmourAndLife1"])

    # the user's text (Ctrl+Alt+C, Russian client): two Eldritch implicits and a crafted hybrid
    RU_ADVANCED = """{ Собственное свойство Пламенного экзарха (Мелкий) — Стихийный, Огонь, Состояние }
Наложенные вами поджоги распространяются на других врагов в радиусе 1.2 м
{ Собственное свойство Пожирателя миров (Мелкий) }
0.2% урона от огня похищается в виде здоровья
{ Ремесленное Свойство-префикс "Улучшенный"  — Здоровье, Защита, Броня, Энергетический щит }
18(18-21)% повышение брони и энергетического щита
+13(12-14) к максимуму здоровья"""

    def ru_item(self, base_en):
        from poecraft import i18n
        b = max((x for x in self.data.bases.values() if x.name == base_en), key=lambda x: x.drop_level)
        name, ok = i18n.game_text("base", b.id, b.name, "ru")
        self.assertTrue(ok)
        return (f"Класс предмета: Перчатки\nРедкость: Редкий\nХватка рока\n{name}\n--------\nУровень предмета: 84\n"
                f"--------\n{self.RU_ADVANCED}\n--------\nПредмет Пламенного экзарха\nПредмет Пожирателя миров\n"), name

    EXARCH = "exarch|gloves_ignite_proliferation_radius"
    EATER = "eater|horrible_ugly_life_leech_from_fire_damage_perhundredthousand"
    HYBRID = "prefix|base_maximum_life,local_armour_and_energy_shield_+%"

    def test_russian_advanced_eldritch_and_crafted_on_warlock_gloves(self):
        text, name = self.ru_item("Warlock Gloves")
        self.assertEqual(name, "Перчатки колдуна")
        r = self.parse(text)
        self.assertEqual(r["base_name"], "Warlock Gloves")
        by_key = {t["key"]: t for t in r["targets"]}
        self.assertEqual(set(by_key), {self.EXARCH, self.EATER})
        self.assertEqual((by_key[self.EXARCH]["eldritch"], by_key[self.EATER]["eldritch"]), ("exarch", "eater"))
        # shown as "1.2 m" and "0.2%" (stored by the game as 12 and 200): imported thresholds are the displayed
        # values; the planner converts them (this used to assert the stored 12 / 200)
        self.assertEqual((by_key[self.EXARCH]["min"], by_key[self.EATER]["min"]), (1.2, 0.2))
        self.assertFalse([w for w in r["warnings"] if w.startswith("Not found in the game data")], r["warnings"])
        # Warlock Gloves are Energy Shield only: a local Armour + ES craft cannot be on them - said so, not "unknown",
        # and the message names the conflict (the base has only ES, the line needs Armour) so a miscopy is noticed
        self.assertTrue(any(w.startswith("Warlock Gloves has only Energy Shield") and "needs Armour" in w
                            and "повышение брони и энергетического щита" in w for w in r["warnings"]))

    def test_russian_advanced_on_armour_es_gloves_all_three(self):
        text, name = self.ru_item("Paladin Gloves")
        r = self.parse(text)
        keys = [t["key"] for t in r["targets"]]
        self.assertEqual(sorted(keys), sorted([self.EXARCH, self.EATER, self.HYBRID]))
        hybrid = next(t for t in r["targets"] if t["key"] == self.HYBRID)
        self.assertEqual(hybrid["min"], 18)                      # the first displayed number: Armour/ES %
        self.assertFalse([w for w in r["warnings"] if "skipped" in w or "Not found" in w], r["warnings"])
        self.assertTrue(any(w.startswith("Crafted modifier imported as a target") for w in r["warnings"]))
        self.assertEqual(r["have"], [])                         # crafted and Eldritch mods are not "have" mods

    def test_displayed_units_round_trip_import_plan_display(self):
        from poecraft.engine.context import stat_scale, to_display, to_stored
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        from poecraft.messages import render
        exarch_stat, eater_stat = self.EXARCH.split("|")[1], self.EATER.split("|")[1]
        self.assertAlmostEqual(stat_scale(exarch_stat), 0.1)                 # "1.2 m" = 12
        self.assertAlmostEqual(stat_scale(eater_stat), 0.001)                # "0.2%" = 200
        self.assertIsNone(stat_scale("base_maximum_life"))                   # most stats: same units
        self.assertAlmostEqual(to_stored(exarch_stat, to_display(exarch_stat, 13)), 13)
        text, _ = self.ru_item("Paladin Gloves")
        r = self.parse(text)
        specs = [{"signature": t["key"], "min": t["min"], **({"eldritch": t["eldritch"]} if t.get("eldritch") else {})}
                 for t in r["targets"]]
        base = max((x for x in self.data.bases.values() if x.name == "Paladin Gloves"), key=lambda x: x.drop_level).id
        raw = Planner(prices=Prices(use_live=False), sims=0).plan(base, 84, [], specs, lang=None)
        en, ru = render(raw, "en"), render(raw, "ru")
        desc = " ".join(t["description"] for t in en["targets"])
        self.assertIn("within # metres (>= 1.2)", desc)
        self.assertIn("Fire Damage Leeched as Life (>= 0.2)", desc)
        self.assertNotIn("(>= 12)", desc)
        self.assertNotIn("(>= 200)", desc)
        self.assertIn("(>= 0.2)", " ".join(t["description"] for t in ru["targets"]))
        # the math used the stored values: the plan's Eldritch steps reach the item's own tiers
        states = " ".join(st["expected_state"] for st in en["strategies"][0]["steps"])
        import re
        metres = float(re.search(r"within ([\d.]+) metres", states).group(1))
        leech = float(re.search(r"([\d.]+)% of Fire Damage Leeched as Life", states).group(1))
        self.assertGreaterEqual(metres, 1.2)                     # a tier that meets the displayed threshold
        self.assertGreaterEqual(leech, 0.2)
        self.assertLess(leech, 1)                                # displayed units, not the stored 200 / 300

    def test_typed_threshold_in_displayed_units_and_mod_list_ranges(self):
        from poecraft import server
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        base = max((x for x in self.data.bases.values() if x.name == "Paladin Gloves"), key=lambda x: x.drop_level).id
        r = Planner(prices=Prices(use_live=False), sims=0).plan(base, 84, [], [
            {"eldritch": "exarch", "signature": self.EXARCH, "min": 1.3}], lang="en")
        self.assertIn("(>= 1.3)", r["targets"][0]["description"])
        step = next(st for st in r["strategies"][0]["steps"] if "Eldritch" in st["title"])
        self.assertIn("1.3 metres", step["expected_state"])                 # 13 stored: the 1.2 m tier is not enough
        fam = next(f for f in server._families({"base": [base], "ilvl": ["84"]})["families"] if f["key"] == self.EXARCH)
        maxima = sorted(t["max"] for t in fam["tiers"])
        self.assertEqual(maxima[0], 1.2)                                     # the UI's mod list: displayed metres
        self.assertLess(fam["max_value"], 5)

    def test_english_advanced_eldritch_header(self):
        from poecraft.data.eldritch import get_eldritch
        from poecraft.engine import eldritch as eld
        base = max((x for x in self.data.bases.values() if x.name == "Paladin Gloves"), key=lambda x: x.drop_level)
        fam = eld.base_families(get_eldritch(), base.item_class, set(base.tags))[self.EXARCH]
        m = fam.tiers[max(fam.tiers)]                            # the Lesser tier: "... within 1.2 metres"
        import re
        line = re.sub(r"\(-?[\d.]+--?[\d.]+\)", lambda x: x.group(0)[1:].split("-")[0], m.text)
        text = (f"Item Class: Gloves\nRarity: Rare\nDoom Grasp\nPaladin Gloves\n--------\nItem Level: 84\n--------\n"
                f"{{ Searing Exarch Implicit Modifier (Lesser) — Elemental, Fire, Ailment }}\n{line}\n")
        r = self.parse(text)
        self.assertEqual([(t["key"], t["eldritch"]) for t in r["targets"]], [(self.EXARCH, "exarch")])

    def test_unique_and_unknown_base(self):
        r = self.parse("Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------\nItem Level: 80\n--------\n+30 to Dexterity")
        self.assertEqual(r["base_name"], "Stealth Boots")
        self.assertEqual(r["targets"], [])
        self.assertTrue(any(w.startswith("Unique item") for w in r["warnings"]))
        r = self.parse("Rarity: Rare\nSomething\nNo Such Base Anywhere\n--------\nItem Level: 80")
        self.assertIsNone(r.get("base_id"))
        self.assertTrue(r["warnings"][0].startswith("Base item not recognised"))

    def test_cli_paste(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from poecraft.__main__ import main
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = main(["plan", "--paste", self.EN.replace("\n", "\\n"), "--sims", "0", "--no-live-prices", "--quiet"])
        text = out.getvalue()
        self.assertIn(rc, (0, None))
        self.assertIn("Imported from item text: Crusader Boots, item level 86; 5 target(s)", text)
        self.assertIn("! Not found in the game data for this base (skipped): Grants Immunity", text)
        self.assertIn("=== RECOMMENDED", text)

    def test_ocr_unconfigured_sends_nothing_and_configured_goes_through_the_parser(self):
        import io
        import json
        import os
        from unittest import mock
        from poecraft import ocr
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POECRAFT_OCR_SPACE_KEY", None)
            self.assertFalse(ocr.status()["available"])
            with mock.patch("urllib.request.urlopen") as net:
                with self.assertRaises(ocr.OcrError):
                    ocr.image_to_text("data:image/png;base64,AAAA")
                net.assert_not_called()                                  # no key: nothing leaves the machine
        fake = io.BytesIO(json.dumps({"IsErroredOnProcessing": False,
                                      "ParsedResults": [{"ParsedText": self.EN}]}).encode())
        with mock.patch.dict(os.environ, {"POECRAFT_OCR_SPACE_KEY": "test-key"}), \
                mock.patch("urllib.request.urlopen", return_value=fake) as net:
            text = ocr.image_to_text("data:image/png;base64,AAAA", "en")
            self.assertEqual(net.call_args[0][0].full_url, ocr.ENDPOINT)
        self.assertIn("Crusader Boots", text)
        self.assertEqual(self.parse(text)["base_name"], "Crusader Boots")


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class EssenceTierSelection(unittest.TestCase):
    """min_cost picks the cheapest essence tier that can be used on the item and can meet the threshold."""
    LIFE = "prefix|base_maximum_life"

    def plan(self, base_name, ilvl, spec, objective="min_cost"):
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        base = get_data().base_by_name(base_name).id
        return Planner(prices=Prices(use_live=False), sims=0).plan(base, ilvl, [], [spec], objective=objective, lang="en")

    @staticmethod
    def essence_rows(r):
        return [c for c in r["comparison"] if str(c["name"]).startswith("Essence spam: ")]

    def test_no_minimum_low_item_level_uses_the_cheapest_essence(self):
        r = self.plan("Simple Robe", 30, {"signature": self.LIFE})
        self.assertEqual(str(r["recommended"]), "Essence spam: Whispering Essence of Greed")
        rows = self.essence_rows(r)
        self.assertEqual(min(rows, key=lambda c: c["cost"])["name"], rows[0]["name"])

    def test_no_minimum_respects_the_item_level_restriction(self):
        r = self.plan("Vaal Regalia", 86, {"signature": self.LIFE})
        names = [str(c["name"]) for c in self.essence_rows(r)]
        for tier in ("Whispering", "Muttering", "Weeping", "Wailing"):      # restricted to item level 35/45/60/75
            self.assertFalse(any(tier in n for n in names), names)
        self.assertEqual(min(self.essence_rows(r), key=lambda c: c["cost"])["name"],
                         "Essence spam: Screaming Essence of Greed")
        self.assertTrue(any(w.startswith("Lower-tier essences are not offered for item level 86") for w in r["warnings"]))
        r70 = self.plan("Vaal Regalia", 70, {"signature": self.LIFE})                   # Wailing allows item level 75
        self.assertEqual(str(r70["recommended"]), "Essence spam: Wailing Essence of Greed")

    def test_minimum_uses_the_cheapest_tier_that_can_meet_it(self):
        r = self.plan("Vaal Regalia", 86, {"signature": self.LIFE, "min": 140})
        self.assertEqual(str(r["recommended"]), "Essence spam: Shrieking Essence of Greed")
        rows = {str(c["name"]): c["cost"] for c in self.essence_rows(r)}
        # Screaming is cheaper per essence but reaches 140 only with Divine Orbs, so it ranks far behind
        self.assertGreater(rows["Essence spam: Screaming Essence of Greed"], rows["Essence spam: Shrieking Essence of Greed"])


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class MainPlanAndBranches(unittest.TestCase):
    """The main plan is a numbered linear list; recovery branches are conditional and hang off their step."""
    TARGETS = [{"signature": "prefix|base_maximum_life", "min": 140}, {"signature": "prefix|local_energy_shield", "min": 50},
               {"signature": "suffix|base_cold_damage_resistance_%", "min": 36},
               {"signature": "suffix|base_lightning_damage_resistance_%", "min": 36}]

    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.base = get_data().base_by_name("Vaal Regalia").id
        cls.raw = Planner(prices=Prices(use_live=False), sims=0).plan(cls.base, 86, [], cls.TARGETS, lang=None)

    def test_main_steps_numbered_and_branches_conditional(self):
        from poecraft.messages import render
        best = render(self.raw, "en")["strategies"][0]
        main = [st for st in best["steps"] if st["kind"] == "main"]
        branches = [st for st in best["steps"] if st["kind"] == "recovery"]
        self.assertEqual([st["number"] for st in main], list(range(1, len(main) + 1)))
        self.assertTrue(branches)
        for st in branches:
            self.assertIsNone(st["number"])
            parent = next(m for m in main if m["number"] == st["branch_of"])
            self.assertTrue(parent["title"].startswith("Reroll"))
            self.assertRegex(st["title"], r"^IF .+: ")
            self.assertIn("otherwise skip this branch", st["detail"])
        self.assertFalse(any(st["title"].startswith("Recovery:") for st in best["steps"]))

    def test_conditions_are_localized(self):
        from poecraft.messages import render
        for lang, word in (("ru", "ЕСЛИ "), ("de", "WENN ")):
            steps = render(self.raw, lang)["strategies"][0]["steps"]
            self.assertTrue(all(st["title"].startswith(word) for st in steps if st["kind"] == "recovery"), lang)

    def test_cli_prints_main_plan_then_recovery(self):
        import io
        import re
        from contextlib import redirect_stderr, redirect_stdout
        from poecraft.__main__ import main
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            main(["plan", "--base", "Vaal Regalia", "--target", "+# to maximum Life>=140",
                  "--target", "+# to maximum Energy Shield>=50", "--target", "Cold Resistance>=36",
                  "--target", "Lightning Resistance>=36", "--sims", "0", "--no-live-prices", "--quiet"])
        text = out.getvalue().split("--- Comparison")[0]
        self.assertIn("--- MAIN PLAN ---", text)
        main_part, rec_part = text.split("--- RECOVERY BRANCHES")
        self.assertNotRegex(main_part, r"\n  \d+\. IF ")                 # no branch inside the numbered list
        self.assertRegex(rec_part, r"R1 \(for step 2\): IF ")
        numbers = [int(n) for n in re.findall(r"\n  (\d+)\. ", main_part)]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class ModSearch(unittest.TestCase):
    """The target picker's search (/api/modsearch): matches on the base, and why a matching mod is missing."""
    AES = "prefix|local_armour_and_energy_shield_+%"

    def search(self, base_name, q, lang="en"):
        from poecraft import server
        from poecraft.data.repository import get_data
        from poecraft.engine.modsearch import mod_search
        data = get_data()
        b = data.base_by_name(base_name)
        fams = server._families_cached({"base": [b.id], "ilvl": ["86"], "inf": [""], "lang": [lang]})["families"]
        return mod_search(data, b, fams, q, lang)

    def ui_message(self, entry, lang="en"):
        """The sentence the UI shows for a mod that cannot roll here (same locale keys, same join)."""
        from poecraft import i18n
        req = "/".join(i18n.t(lang, f"def.{d}") for d in entry["requires"])
        return i18n.t(lang, "mods.cannotRoll", req=req)

    def test_valid_str_int_boots_find_the_hybrid(self):
        r = self.search("Crusader Boots", "increased armour and energy shield")
        self.assertEqual(r["base_defence"], ["armour", "energy_shield"])
        self.assertEqual(r["matches"][0], self.AES)                 # the exact mod first
        self.assertNotIn(self.AES, [e["signature"] for e in r["elsewhere"]])

    def test_evasion_boots_explain_why_the_hybrid_is_missing(self):
        r = self.search("Velour Boots", "increased armour and energy shield")
        self.assertEqual(r["base_defence"], ["evasion"])
        self.assertNotIn(self.AES, r["matches"])
        miss = next(e for e in r["elsewhere"] if e["signature"] == self.AES)
        self.assertEqual(miss["requires"], ["armour", "energy_shield"])
        self.assertTrue(miss["examples"])
        self.assertEqual(self.ui_message(miss),
                         "This mod exists, but cannot roll on this base type (requires Armour/Energy Shield base).")

    def test_other_defence_combinations(self):
        cases = [("Velour Boots", "increased armour and evasion", "prefix|local_armour_and_evasion_+%", ["armour", "evasion"]),
                 ("Crusader Boots", "increased evasion and energy shield", "prefix|local_evasion_and_energy_shield_+%",
                  ["evasion", "energy_shield"]),
                 ("Velour Boots", "increased energy shield", "prefix|local_energy_shield_+%", ["energy_shield"])]
        for base, q, sig, need in cases:
            with self.subTest(base=base, q=q):
                r = self.search(base, q)
                self.assertNotIn(sig, r["matches"])
                miss = next(e for e in r["elsewhere"] if e["signature"] == sig)
                self.assertEqual(miss["requires"], need)
                # and on a base that has those defences it is a normal match
                ok_base = {"armour,evasion": "Wyvernscale Boots", "evasion,energy_shield": "Fugitive Boots",
                           "energy_shield": "Sorcerer Boots"}[",".join(need)]
                self.assertIn(sig, self.search(ok_base, q)["matches"])

    def test_russian_query_and_message(self):
        r = self.search("Velour Boots", "повышение брони и энергетического щита", "ru")
        miss = next(e for e in r["elsewhere"] if e["signature"] == self.AES)
        self.assertIn("брони и энергетического щита", miss["label"])           # the game's own Russian text
        self.assertEqual(self.ui_message(miss, "ru"),
                         "Этот модификатор существует, но не может выпасть на этом типе базы "
                         "(нужна база: Броня/Энергетический щит).")


@unittest.skipUnless(config.DB_PATH.exists(), "database not built - run `python -m poecraft update`")
class Recovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.data.repository import get_data, get_rules
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        cls.data = get_data()
        cls.base = cls.data.base_by_name("Vaal Regalia")
        ctx = ItemContext(cls.data, get_rules(), cls.base, 86, [])
        cls.fams = {f.label: f.signature for f in ctx.families().values()}
        # fixed config prices so the expectations do not drift with the live market
        cls.planner = Planner(prices=Prices(use_live=False), sims=0)

    def spec(self, label, mn):
        return {"signature": self.fams[label], "min": mn}

    def test_annul_is_the_recovery_for_two_good_one_bad(self):
        # Life T1 + Cold T1 are good; a +6-11% Lightning Resistance misses the >=25 target AND blocks the
        # bench craft (same mod group). Only removing it helps: Annul, 1 in 3.
        specs = [self.spec("+# to maximum Life", 160), self.spec("+#% to Cold Resistance", 42),
                 self.spec("+#% to Lightning Resistance", 25)]
        r = self.planner.recover(self.base.id, 86, [], specs, ["IncreasedLife12", "ColdResist8", "LightningResist1"])
        best = r["options"][0]
        self.assertIn("annul", best["key"])
        self.assertEqual(best["status"], "computed")
        self.assertAlmostEqual(best["p_progress"], 1 / 3, places=9)
        restart = next(o for o in r["options"] if o["key"] == "restart")
        self.assertLess(best["expected_total"], restart["expected_total"])
        step = next(st for st in r["steps"] if st["title"].startswith("Recovery"))
        self.assertAlmostEqual(step["probability"], 1 / 3, places=9)
        self.assertTrue(step["on_fail"])

    def test_prefixes_locked_scour_keeps_prefix_and_prices_refill(self):
        specs = [self.spec("+# to maximum Life", 160), self.spec("+#% to Cold Resistance", 42),
                 self.spec("+#% to Lightning Resistance", 42)]
        r = self.planner.recover(self.base.id, 86, [], specs, ["IncreasedLife12", "StunRecovery6", "ChaosResist6"])
        o = next(o for o in r["options"] if o["key"] == "lock_scour_prefix")
        self.assertEqual(o["status"], "computed")
        self.assertEqual(o["p_keep_targets"], 1.0)                 # the Life prefix always survives
        self.assertEqual(len(o["outcomes"]), 1)
        self.assertIn("1 prefix(es) / 0 suffix(es)", o["outcomes"][0]["state"])
        self.assertAlmostEqual(o["action_cost"], 2 * 150 + 0.5)    # 2 Divine (metacraft) + Scouring, config prices
        refill = o["cost_to_finish_this_item"]                     # Exalt/Annul refill of both suffixes
        self.assertIsNotNone(refill)
        self.assertGreater(refill, o["action_cost"])

    def worked_specs(self):
        return [self.spec("+# to maximum Life", 140), self.spec("+# to maximum Energy Shield", 50),
                self.spec("+#% to Cold Resistance", 36), self.spec("+#% to Lightning Resistance", 36)]

    def test_unlisted_essence_is_marked_estimated_with_live_prices(self):
        import json
        import tempfile
        import time
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        live = {"_meta": {"league": "TestLeague", "fetched_at": now, "source": "https://poe.ninja/poe1/api/economy", "last_attempt": {"ok": True, "selection_verified": True}},
                "prices": {"Chaos Orb": 1, "Exalted Orb": 5, "Divine Orb": 300, "Orb of Annulment": 10}}
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "live.json"
            p.write_text(json.dumps(live))
            result = Planner(prices=Prices(live_path=p), sims=0).plan(self.base.id, 86, [], self.worked_specs())
        essences = {s["name"].split("Essence spam: ")[1].split(" +")[0]
                    for s in result["strategies"] if s["name"].startswith("Essence spam: ")}
        self.assertTrue(essences, "worked example should show at least one essence strategy")
        table = {k["name"]: k["source"] for k in result["prices"]["key_prices"]}
        for name in essences:      # none of them is in the live snapshot above
            self.assertTrue(any(w.startswith(f"Price estimate: {name} is not listed on poe.ninja (TestLeague)")
                                for w in result["warnings"]), name)
            self.assertEqual(table.get(name), "estimated")

    def test_alteration_recovery_reaches_attempt_target(self):
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        result = Planner(prices=Prices(use_live=False), sims=400).plan(self.base.id, 86, [], self.worked_specs())
        alt = [s for s in result["strategies"] if s["family"] == "alt+rec"]
        self.assertTrue(alt, "Alteration + recovery should be among the shown strategies")
        self.assertEqual(alt[0]["sim"]["n"], 400)
        self.assertNotIn("low_confidence", alt[0]["sim"])

    def test_recover_baseline_is_calculated_and_choice_unchanged(self):
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        specs = [self.spec("+# to maximum Life", 160), self.spec("+#% to Cold Resistance", 42),
                 self.spec("+#% to Lightning Resistance", 25)]
        have = ["IncreasedLife12", "ColdResist8", "LightningResist1"]
        fast = Planner(prices=Prices(use_live=False), sims=300).recover(self.base.id, 86, [], specs, have)
        full = Planner(prices=Prices(use_live=False), sims=300).recover(self.base.id, 86, [], specs, have,
                                                                        full_sims=True)
        self.assertEqual(fast["fresh_plan"]["basis"], "calculated")
        self.assertIsNone(fast["fresh_plan"]["simulation"])          # baseline not simulated by default
        self.assertIsNotNone(full["fresh_plan"]["simulation"])       # --full-sims honoured
        self.assertEqual(fast["fresh_plan"]["expected_cost"], full["fresh_plan"]["expected_cost"])
        self.assertEqual([(o["key"], o["expected_total"]) for o in fast["options"]],
                         [(o["key"], o["expected_total"]) for o in full["options"]])

    def test_min_variance_recommendation_is_defensible_from_table(self):
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        result = Planner(prices=Prices(use_live=False), sims=300).plan(self.base.id, 86, [], self.worked_specs(),
                                                                        objective="min_variance")
        self.assertEqual(result["objective"]["key"], "min_variance")
        rows = result["comparison"]
        self.assertEqual(len(rows), result["considered"])                # every evaluated variant is listed
        rec = next(r for r in rows if r["why"] == "RECOMMENDED")
        self.assertEqual(rec["name"], result["recommended"])
        self.assertEqual(result["strategies"][0]["name"], result["recommended"])
        for r in rows:                                                   # nothing simulated-and-agreeing scores better
            if r["simulated"] and not r["why"].startswith("demoted"):
                self.assertGreaterEqual(r["bad_luck_cost"], rec["bad_luck_cost"] - 1e-9)

    def test_low_confidence_flag(self):
        from poecraft.engine.planner import Planner
        res = Planner._sim_json(0.01, 28, 2893)
        Planner._flag_low_confidence(res, 20000)
        self.assertTrue(res["low_confidence"].startswith("only 2,893 simulated attempts (target 20,000)"))
        full = Planner._sim_json(0.01, 210, 20000)
        Planner._flag_low_confidence(full, 20000)
        self.assertNotIn("low_confidence", full)

    def test_honesty_flags_for_formerly_unflagged_limits(self):
        from poecraft.data.repository import get_rules
        from poecraft.engine.context import ItemContext
        from poecraft.engine.planner import Planner
        from poecraft.engine.prices import Prices
        # 1. Bloodstained / Opulent are in the feature list (UI card and CLI), each on its own row with its reason
        from poecraft.data.special import get_special
        from poecraft.engine import special
        rows = {f["name"]: f for f in special.feature_rows(get_special(), get_rules())}
        for name in ("Bloodstained Fossil", "Opulent Fossil"):
            self.assertTrue(rows[name]["status"].startswith("not supported ("))
            self.assertTrue(rows[name]["note"])
        # 4. inference applies to local defence crafts only
        mods = self.planner.data.mods
        self.assertTrue(ItemContext.bench_inferred(mods["EinharMasterFlatEnergyShield2"]))
        self.assertFalse(ItemContext.bench_inferred(mods["HelenaMasterIncreasedLife1"]))
        result = Planner(prices=Prices(use_live=False), sims=0).plan(self.base.id, 86, [], self.worked_specs())
        # 2. one base-price line per plan
        self.assertEqual(result["cost_note"], "Base item price is not included.")
        # 3. Harvest shown -> its config price is named as an estimate
        if any("Harvest" in s["name"] for s in result["strategies"]):
            self.assertTrue(any(w.startswith("Price estimate: Harvest Reforge (tag) has no live price")
                                for w in result["warnings"]))
        best = result["strategies"][0]
        bench = next(st for st in best["steps"] if st["title"] == "Crafting bench")   # local Energy Shield craft
        self.assertEqual(bench["status"], "approximate")
        self.assertIn("bench craft eligibility is inferred from the base's defence types; the export has no spawn "
                      "tags for bench crafts", bench["detail"])

    def test_worked_example_still_plans(self):
        specs = [self.spec("+# to maximum Life", 140), self.spec("+# to maximum Energy Shield", 50),
                 self.spec("+#% to Cold Resistance", 36), self.spec("+#% to Lightning Resistance", 36)]
        result = self.planner.plan(self.base.id, 86, [], specs)
        names = {s["name"]: s["expected_cost"] for s in result["strategies"]}
        plain = names["Essence spam: Screaming Essence of Hatred + bench craft"]
        # was 253.2 = 149.5 + 30.0 optional Divines + ~73.7 Orbs of Scouring (Screaming reforges a rare directly)
        self.assertAlmostEqual(plain, 149.5, delta=0.1)
        s0 = next(s for s in result["strategies"] if s["name"] == "Essence spam: Screaming Essence of Hatred + bench craft")
        self.assertAlmostEqual(s0["optional_divine"]["cost"], 30.0, delta=0.1)
        self.assertAlmostEqual(s0["optional_divine"]["total_with"], plain + 30.0, delta=0.1)
        best = result["strategies"][0]
        self.assertLessEqual(best["expected_cost"], plain)
        if "recovery" in best:
            self.assertIsNotNone(result["recovery_summary"])
            self.assertIn("beats the best plan without it", best["notes"][0])


if __name__ == "__main__":
    unittest.main()
