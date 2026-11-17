import unittest
from dataclasses import replace
from types import SimpleNamespace

from poecraft.data.repository import Mod, Stat
from poecraft.engine.context import PoolEntry
from poecraft.engine.analytic import GroupedPool, RecoveryMDP, RESTART
from poecraft.engine.planner import Planner
from poecraft.engine.prices import Prices
from poecraft.engine.simulate import Simulator
from poecraft.messages import render


def ordinary(mid, side):
    return Mod(mid, mid, 'item', side, mid, 1, False, mid, (mid,), (),
               (('default', 100),), (), (Stat(mid, 1, 1),))


class PartialLocks(unittest.TestCase):
    def test_simulation_preserves_all_within_cap_and_rejects_excess(self):
        planner = object.__new__(Planner)
        for side in ('prefix','suffix'):
            for cap in (1,2):
                mods,gp,state,mdp = self.model(side,cap)
                action = next(a for a,_ in mdp._actions(state) if a.key == f'lock_annul_{cap}_{side}')
                sim = Simulator([PoolEntry(m,100,1) for m in mods], seed=31)
                for _ in range(40):
                    out = planner._apply(sim,action,mods,(),{'prefix':3,'suffix':3})
                    self.assertEqual([m for m in out if m.affix == side], [m for m in mods if m.affix == side])
                too_many = mods + [ordinary('excess',side)]
                self.assertIsNone(planner._apply(sim,action,too_many,(),{'prefix':3,'suffix':3}))

    def test_costs_are_not_reused_after_source_data_changes(self):
        p = Planner(prices=Prices(use_live=False), sims=0)
        p._mdp_costs('Body Armour')
        p.data = SimpleNamespace(mods=p.data.mods, bench_crafts=[])
        self.assertFalse(any(k.startswith('partial_') for k in p._mdp_costs('Body Armour')))

    def model(self, side, count, other=1, special=False, fractured=False):
        opposite = 'suffix' if side == 'prefix' else 'prefix'
        mods = [ordinary('keep'+str(i), side) for i in range(count)] + [ordinary('drop'+str(i), opposite) for i in range(other)]
        if special:
            mods[0] = replace(mods[0], domain='crafted')
        protected = (mods[0],) if fractured else ()
        gp = GroupedPool([PoolEntry(m, 100, 1) for m in mods], [], track=tuple(mods), protected=protected)
        costs = dict(exalt=1, annul=1, scour=1, chaos=1, remove_crafted=1, meta_p=1000, meta_s=1000)
        costs.update({f'partial_{s}_{n}': 100*n for s in ('prefix', 'suffix') for n in (1, 2)})
        return mods, gp, gp.state_of(mods), RecoveryMDP(gp, {'prefix':3, 'suffix':3}, {4:1}, costs, 0, lambda s:False)

    def test_four_variants_and_counts_one_two_three(self):
        for side in ('prefix', 'suffix'):
            for count in (1, 2, 3):
                for cap in (1, 2):
                    with self.subTest(side=side, count=count, cap=cap):
                        _, gp, state, mdp = self.model(side, count)
                        actions = {a.key:(a,outs) for a,outs in mdp._actions(state)}
                        key = f'lock_annul_{cap}_{side}'
                        self.assertEqual(key in actions, count <= cap)
                        if count <= cap:
                            a, outs = actions[key]
                            self.assertAlmostEqual(a.cost, 100*cap + 1 + .5)
                            self.assertAlmostEqual(sum(p for p,st in outs if st == state), .5)  # craft can be annulled
                            self.assertTrue(all(st != RESTART and gp.counts(st)[side == 'suffix'] == count for p,st in outs))
                        self.assertIn('lock_annul_'+side, actions)  # full lock remains a distinct option

    def test_slots_crafted_and_fractured_are_not_assumed_safe(self):
        for args in ({'other':3}, {'special':True}, {'fractured':True}):
            for side in ('prefix','suffix'):
                _,_,state,mdp = self.model(side, 1, **args)
                self.assertFalse(any(a.key.startswith(('lock_annul_1_', 'lock_annul_2_')) for a,_ in mdp._actions(state)))

    def test_no_partial_scour_chaos_or_harvest_and_recheck_next_state(self):
        _,gp,state,mdp = self.model('prefix', 3)
        self.assertFalse(any(a.key.startswith(('lock_annul_1_', 'lock_annul_2_')) and a.key.endswith('prefix') for a,_ in mdp._actions(state)))
        _,outs = gp.annul(state)
        smaller = next(st for _,st in outs if st != RESTART and gp.counts(st)[0] == 2)
        self.assertTrue(any(a.key == 'lock_annul_2_prefix' for a,_ in mdp._actions(smaller)))
        self.assertFalse(any('partial' in a.key or a.key.startswith(('lock_scour_1_', 'lock_chaos_1_')) for a,_ in mdp._actions(smaller)))

    def test_labels_are_localized_and_do_not_claim_full_lock(self):
        p = object.__new__(Planner)
        for lang in ('en','ru','de'):
            for side in ('prefix','suffix'):
                for cap in (1,2):
                    text = render(p._act_label(f'lock_annul_{cap}_{side}'),lang)
                    self.assertIn(str(cap),text)
                    self.assertNotIn('m.act.',text)

    def test_current_export_controls_availability_and_prices(self):
        p = Planner(prices=Prices(use_live=False), sims=0)
        for cls in ('Bow','Body Armour','Ring'):
            costs = p._mdp_costs(cls)
            for side in ('prefix','suffix'):
                for cap in (1,2):
                    recipes = [b for b in p.data.bench_crafts if cls in b.item_classes and b.mod_id in p.data.mods
                               and any(s.id == f'item_generation_cannot_change_x_{side}es' and s.min == s.max == cap
                                       for s in p.data.mods[b.mod_id].stats)]
                    key = f'partial_{side}_{cap}'
                    self.assertEqual(key in costs, bool(recipes))
                    if recipes:
                        self.assertEqual(costs[key], p.prices.get('Divine Orb') * cap)
