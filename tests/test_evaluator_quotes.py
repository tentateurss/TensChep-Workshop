import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import urllib.error

from poecraft.data import ninja, trade
from poecraft.engine import evaluator as E, prices
from poecraft.messages import render
from poecraft import i18n
import test_trade_metrics as fixtures

Prices = prices.Prices
TEXT = 'Item Class: Stackable Currency\nRarity: Currency\nChaos Orb\n--------\nStack Size: 10/20'


class ExchangeQuotes(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TradeMetrics()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = Path(self.fixture.tmp.name) / 'prices.json'
        self.snapshot()
        factory = patch.object(prices, 'Prices', side_effect=lambda *a, **kw: Prices(
            live_path=self.path, league=kw.get('league')))
        factory.start()
        self.addCleanup(factory.stop)

    def snapshot(self, league='Covens', age=0, ok=True, verified=True, values=None):
        self.path.write_text(json.dumps({'_meta': {
            'source': ninja.API, 'league': league,
            'fetched_at': ninja._now_iso(time.time() - age),
            'last_attempt': {'ok': ok, 'selection_verified': verified}},
            'prices': values if values is not None else {'Chaos Orb': 1, 'Deafening Essence of Greed': 3}}), encoding='utf-8')

    def test_cold_and_repeat_skip_all_trade_requests(self):
        for _ in range(2):
            result = E.evaluate(TEXT, league='Covens')
            self.assertEqual(result['quote']['unit_chaos'], 1)
            self.assertEqual(result['quote']['total_chaos'], 10)
            self.assertIsNone(result['summary'])
            self.assertFalse(result['listings'])
            self.assertFalse(self.fixture.sent)
            self.assertEqual(result['trade_metrics']['evaluation.quote_hit'], 1)

    def test_consumable_and_supported_client_languages(self):
        for name in ('Chaos Orb', 'Deafening Essence of Greed'):
            result = E.evaluate(TEXT.replace('Chaos Orb', name), league='Covens')
            for language in ('en', 'ru', 'de'):
                presented = render(result, language)
                self.assertEqual(presented['quote']['source'], 'poe.ninja')
            self.assertIn('quote', result)
        self.assertFalse(self.fixture.sent)

    def test_russian_and_german_item_text(self):
        for language, header, rarity, stack in (
                ('ru', 'Класс предмета: Валюта', 'Редкость: Валюта', 'Размер стопки'),
                ('de', 'Gegenstandsklasse: Stapelbare Währung', 'Seltenheit: Währung', 'Stapelgröße')):
            name, _ = i18n.currency_text('Chaos Orb', language)
            text = f'{header}\n{rarity}\n{name}\n--------\n{stack}: 10/20'
            self.assertEqual(E.evaluate(text, league='Covens')['quote']['total_chaos'], 10)
        self.assertFalse(self.fixture.sent)

    def test_manual_listings_use_existing_search_and_cache(self):
        E.evaluate(TEXT, league='Covens')
        for _ in range(2):
            result = E.evaluate(TEXT, league='Covens', load_listings=True)
            self.assertNotIn('quote', result)
            self.assertTrue(result['listings'])
        self.assertEqual(self.fixture.sent['search'], 1)
        self.assertEqual(self.fixture.sent['fetch'], 2)

    def test_wrong_league_missing_stale_and_unverified_fall_back(self):
        for options in ({'league': 'Standard'}, {'age': ninja.MAX_CACHE + 10},
                        {'ok': False}, {'verified': False}, {'values': {'Divine Orb': 300}}):
            with self.subTest(options=options):
                self.snapshot(**options)
                result = E.evaluate(TEXT, league='Covens')
                self.assertNotIn('quote', result)
                self.assertNotIn('ninja_price', result)
                self.assertTrue(result['passes'])
        self.assertGreater(self.fixture.sent['search'], 0)
        self.path.unlink()
        self.assertNotIn('quote', E.evaluate(TEXT, league='Covens'))

    def test_variant_and_unknown_name_do_not_reuse_plain_quote(self):
        for text in (TEXT + '\nQuality: +20%', TEXT.replace('Chaos Orb', 'Unknown Orb'),
                     TEXT.replace('10/20', './20')):
            result = E.evaluate(text, league='Covens')
            self.assertNotIn('quote', result)
            self.assertNotIn('ninja_price', result)

    def test_snapshot_change_and_league_switch_cannot_reuse_quote(self):
        self.assertEqual(E.evaluate(TEXT, league='Covens')['quote']['unit_chaos'], 1)
        self.assertNotIn('quote', E.evaluate(TEXT, league='Standard'))
        self.snapshot(values={'Chaos Orb': 2})
        self.assertEqual(E.evaluate(TEXT, league='Covens')['quote']['unit_chaos'], 2)

    def test_automatic_league_keeps_existing_selection_rules(self):
        with patch.object(E, '_price_league', return_value=None):
            self.assertEqual(E.evaluate(TEXT)['quote']['league'], 'Covens')
            self.snapshot(league='Standard')
            self.assertNotIn('quote', E.evaluate(TEXT))

    def test_fresh_quote_works_offline_and_during_trade_cooldown(self):
        with patch.object(trade.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')):
            trade._blocked_until = time.monotonic() + 120
            self.assertIn('quote', E.evaluate(TEXT, league='Covens'))
        self.assertFalse(self.fixture.sent)

    def test_absent_quote_and_429_or_network_error_are_honest(self):
        self.snapshot(values={'Divine Orb': 300})
        for mode in ('429', 'offline'):
            E._CACHE.clear()
            trade._reset_limits()
            self.fixture.mode = '429'
            if mode == 'offline':
                context = patch.object(trade.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline'))
            else:
                context = patch.object(E, '_wait', return_value=0)
            with context:
                result = E.evaluate(TEXT, league='Covens')
            self.assertNotIn('quote', result)
            self.assertFalse(result['listings'])
            self.assertTrue(result['warnings'])
