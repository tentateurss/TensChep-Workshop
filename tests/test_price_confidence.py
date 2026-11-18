import json
from pathlib import Path
import tempfile
import unittest

from poecraft.engine.prices import Prices
from poecraft.engine import evaluator
from poecraft.messages import M
from poecraft import i18n


class PriceConfidence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'prices.json'
        self.path.write_text(json.dumps({'Chaos Orb': 1, 'Divine Orb': 150,
                                         'Harvest Reforge (tag)': 3, 'unknown': 1,
                                         'default_essence_by_level': {'7': 6}}))
        self.prices = Prices(self.path, use_live=False)

    def market(self, state='live'):
        self.prices.live = {'Divine Orb': 200}
        self.prices.live_status.update(state=state, league='Covens')

    def test_confirmed_quotes_and_chaos_unit(self):
        self.market()
        self.assertEqual(self.prices.get('Divine Orb'), 200)
        self.prices.get('Chaos Orb')
        self.assertEqual(self.prices.assessment()['state'], 'market')

    def test_harvest_config_is_not_a_confirmed_market_cost(self):
        self.market()
        self.assertEqual(self.prices.get('Harvest Reforge (tag)'), 3)
        self.assertEqual(self.prices.assessment()['state'], 'approximate')

    def test_unknown_placeholder_stays_explicit(self):
        self.assertEqual(self.prices.get('Unpriced resource'), 1)
        self.assertEqual(self.prices.assessment()['state'], 'unknown')
        self.assertEqual(self.prices.assessment()['inputs'][0]['name'], 'Unpriced resource')

    def test_old_same_league_quote_is_not_current(self):
        self.market('stale')
        self.prices.get('Divine Orb')
        self.assertEqual(self.prices.assessment()['state'], 'approximate')

    def test_reference_price_table_does_not_pollute_cost_assessment(self):
        self.market()
        self.prices.get('Divine Orb')
        before = self.prices.assessment()
        self.prices.status_json()
        self.assertEqual(self.prices.assessment(), before)

    def test_essence_tier_fallback_is_approximate(self):
        self.prices.essence('Missing Essence', 7)
        self.assertEqual(self.prices.assessment()['state'], 'approximate')

    def test_essence_without_a_tier_price_is_unknown(self):
        self.prices.essence('Unknown Essence', 99)
        self.assertEqual(self.prices.assessment()['state'], 'unknown')
        row = self.prices.status_json(used_essences=['Unknown Essence'])['key_prices'][-1]
        self.assertEqual(row['source'], 'default')

    def listing(self, currency):
        return evaluator._listing(self.prices, {'item': {}, 'listing': {
            'price': {'amount': 2, 'currency': currency}, 'account': {'online': True}}})

    def test_estimated_conversion_does_not_create_reliable_valuation(self):
        rows = [self.listing('divine') for _ in range(3)]
        result = evaluator._summary({'kind': 'rare', 'listings': rows})
        self.assertFalse(result['reliable_estimate'])
        self.assertEqual(result['reliability_reason'], 'no_conversion')
        self.assertEqual(result['price']['amount'], 2)  # preserve the actual listed price

    def test_live_conversion_retains_existing_valuation(self):
        self.market()
        result = evaluator._summary({'kind': 'rare', 'listings': [self.listing('divine') for _ in range(3)]})
        self.assertTrue(result['reliable_estimate'])
        self.assertEqual(result['chaos'], 400)

    def test_player_messages_hide_details_but_preserve_diagnostics(self):
        message = M('m.price.unavailable', error='private/cache/source.json: stat_id_123')
        for lang in ('en', 'ru', 'de'):
            text = message.render(lang)
            self.assertNotIn('source.json', text)
            self.assertNotIn('stat_id', text)
            self.assertEqual(message.params['error'], 'private/cache/source.json: stat_id_123')
            for key in ('m.price.ranking_approximate', 'm.price.ranking_unknown',
                        'prices.totalApprox', 'prices.conditionalRank', 'm.eval.price_conversion_estimate'):
                self.assertFalse(i18n.lookup(lang, key)[1], (lang, key))
            self.assertTrue(i18n.currency_text('Harvest Reforge (tag)', lang)[1])
            self.assertNotIn('(tag)', i18n.currency_text('Harvest Reforge (tag)', lang)[0])
            for key in ('m.warn.essence_ilvl', 'm.rule.remove_crafted_note', 'm.eld.ilvl',
                        'm.fx.MoreGemLevel', 'm.fx.AbyssSocket', 'gamedata.marker'):
                template, fallback = i18n.lookup(lang, key)
                self.assertFalse(fallback)
                for internal in ('item_level_restriction', 'remove_crafted_mods', 'required_level',
                                 'added_mods', 'forced_mods', '.json'):
                    self.assertNotIn(internal, template)
