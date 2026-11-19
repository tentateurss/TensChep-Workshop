import unittest

from poecraft.engine import evaluator as E
import test_evaluator_quotes as quotes


class DisplayRate(unittest.TestCase):
    def setUp(self):
        self.fixture = quotes.ExchangeQuotes()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_verified_fresh_rate_without_trade_requests(self):
        self.fixture.snapshot(values={'Divine Orb': 200, 'Chaos Orb': 1})
        for _ in range(2):
            result = E.evaluate(quotes.TEXT, league='Covens')
            rate = result['display_rate']
            self.assertEqual(rate['chaos_per_divine'], 200)
            self.assertEqual(rate['league'], 'Covens')
            self.assertEqual(rate['source'], 'poe.ninja')
            self.assertEqual(result['quote']['total_chaos'], 10)
        self.assertFalse(self.fixture.fixture.sent)

    def test_missing_stale_wrong_league_or_unverified_rate_is_not_exposed(self):
        for options in ({'age': 2000}, {'league': 'Standard'}, {'ok': False}, {'verified': False},
                        {'values': {'Chaos Orb': 1}}, {'values': {'Divine Orb': 0, 'Chaos Orb': 1}}):
            with self.subTest(options=options):
                self.fixture.snapshot(**options)
                self.assertIsNone(E.evaluate(quotes.TEXT, league='Covens').get('display_rate'))

    def test_listing_search_budget_unchanged(self):
        self.fixture.snapshot(values={'Divine Orb': 200, 'Chaos Orb': 1})
        for _ in range(2):
            result = E.evaluate(quotes.TEXT, league='Covens', load_listings=True)
            self.assertEqual(result['display_rate']['chaos_per_divine'], 200)
            self.assertTrue(result['listings'])
        self.assertEqual(self.fixture.fixture.sent['search'], 1)
        self.assertEqual(self.fixture.fixture.sent['fetch'], 2)
