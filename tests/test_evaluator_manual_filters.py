from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import json
import threading
import unittest
from unittest.mock import patch

from poecraft.data import trade
from poecraft.engine import evaluator as E
import test_trade_metrics as metrics
import test_engine as items

TEXT = items.ItemEvaluator.BOOTS


class ManualFilters(unittest.TestCase):
    def setUp(self):
        self.fixture = metrics.TradeMetrics()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def options(self, result):
        return [{k: f[k] for k in ('id', 'enabled', 'min', 'max')} for f in result['filters']]

    def prepare(self):
        result = E.evaluate(TEXT, league='Covens')
        options = self.options(result)
        life = next(i for i, f in enumerate(result['filters']) if 'maximum Life' in str(f['label']))
        options[life].update(min=70, max=90)
        self.fixture.sent.clear()
        trade._reset_limits()
        return result, options, life

    def test_automatic_relaxation_is_visible_and_unchanged(self):
        self.fixture.mode = 'relaxed'
        result = E.evaluate(TEXT, league='Covens')
        self.assertEqual(self.fixture.sent['search'], 3)
        disabled = [f for f in result['filters'] if not f['enabled']]
        self.assertTrue(disabled)
        self.assertTrue(all(f['reason'].key == 'eval.filters.autoOff' for f in disabled))
        self.assertFalse(result.get('manual_filters'))

    def test_manual_disable_and_bounds_produce_exact_trade_filters(self):
        original, options, life = self.prepare()
        for i, f in enumerate(options):
            f['enabled'] = i == life
        requests = []

        def api(req, **kwargs):
            if '/search/' in req.full_url:
                requests.append(json.loads(req.data))
            return self.fixture.api(req, **kwargs)

        with patch.object(trade.urllib.request, 'urlopen', side_effect=api):
            result = E.evaluate(TEXT, league='Covens', manual_filters=options)
        self.assertEqual(len(requests), 1)
        filters = [f for g in requests[0]['query']['stats'] for f in g['filters']]
        self.assertEqual(filters, [{'id': 's.life', 'value': {'min': 70, 'max': 90}}])
        self.assertEqual(requests[0]['query']['type'], 'Crusader Boots')
        self.assertNotIn('misc_filters', requests[0]['query']['filters'])
        self.assertTrue(result['manual_filters'])
        self.assertEqual(result['filters'][life]['min'], 70)
        self.assertEqual(result['filters'][life]['max'], 90)
        self.assertTrue(result['passes'][0]['dropped'])
        self.assertNotEqual(original['filters'][life]['min'], 70)

    def test_magic_text_uses_same_safe_controls(self):
        text = ('Item Class: Boots\nRarity: Magic\nFecund Crusader Boots of the Volcano\n--------\n'
                'Item Level: 86\n--------\n+82 to maximum Life\n+40% to Fire Resistance')
        result = E.evaluate(text, league='Covens')
        self.assertEqual(result['item']['rarity'], 'magic')
        self.assertTrue(result['filters'])
        manual = E.evaluate(text, league='Covens', manual_filters=self.options(result))
        self.assertTrue(manual['listings'])

    def test_manual_empty_results_do_not_relax_enabled_filters(self):
        _, options, _ = self.prepare()
        self.fixture.mode = 'empty'
        result = E.evaluate(TEXT, league='Covens', manual_filters=options)
        self.assertLessEqual(self.fixture.sent['search'], 2)
        self.assertEqual([p['status'] for p in result['passes']], ['online', 'any'])
        self.assertTrue(all(not p['dropped'] for p in result['passes']))
        self.assertTrue(all(f['enabled'] for f in result['filters']))

    def test_429_does_not_retry_or_lose_controls(self):
        _, options, _ = self.prepare()
        self.fixture.mode = '429'
        with patch.object(E, '_wait', return_value=0):
            result = E.evaluate(TEXT, league='Covens', manual_filters=options)
        self.assertEqual(self.fixture.sent['search'], 1)
        self.assertTrue(result['filters'])
        self.assertTrue(result['warnings'])
        self.assertFalse(E._INFLIGHT)

    def test_bad_ranges_unknown_ids_and_injections_are_rejected(self):
        _, options, life = self.prepare()
        cases = [[], [{'id': 'url', 'enabled': True}], copy.deepcopy(options), copy.deepcopy(options),
                 copy.deepcopy(options), copy.deepcopy(options)]
        cases[2][life].update(min=100, max=1)
        cases[3][life]['id'] = '0' * 64
        cases[4][life]['min'] = float('nan')
        cases[5][life]['min'] = '70'
        for invalid in cases:
            with self.subTest(invalid=str(invalid)[:80]), self.assertRaises(ValueError):
                E.evaluate(TEXT, league='Covens', manual_filters=invalid)
            self.assertFalse(E._INFLIGHT)
        for unsupported in (TEXT + '\nUnidentified', 'Item Class: Boots\nRarity: Rare\nUnknown Base\n--------'):
            with self.assertRaises(ValueError):
                E.evaluate(unsupported, league='Covens', manual_filters=options)
        self.assertEqual(self.fixture.sent['search'], 0)

    def test_cache_isolates_ranges_leagues_and_normalizes_order(self):
        _, options, life = self.prepare()
        E.evaluate(TEXT, league='Covens', manual_filters=options)
        again = E.evaluate(TEXT, league='Covens', manual_filters=list(reversed(options)))
        self.assertEqual(again['trade_metrics']['evaluation.cache_hit'], 1)
        options[life]['min'] = 71
        E.evaluate(TEXT, league='Covens', manual_filters=options)
        E.evaluate(TEXT, league='Standard', manual_filters=options)
        self.assertEqual(self.fixture.sent['search'], 3)

    def test_all_disabled_is_marked_as_category_comparison(self):
        _, options, _ = self.prepare()
        for f in options:
            f['enabled'] = False
        result = E.evaluate(TEXT, league='Covens', manual_filters=options)
        self.assertEqual(result['summary']['match_quality'], 'category')
        self.assertFalse(result['summary']['reliable_estimate'])

    def test_unmapped_mod_cannot_be_manually_enabled(self):
        index = trade.stat_index()
        del index['explicit']['+# to maximum Life']
        with patch.object(trade, 'stat_index', return_value=index):
            result = E.evaluate(TEXT, league='Covens')
            life = next(f for f in result['filters'] if 'maximum Life' in str(f['label']))
            self.assertFalse(life['editable'])
            self.assertFalse(life['enabled'])
            self.assertEqual(life['reason'].key, 'eval.filters.unmapped')
            options = self.options(result)
            next(f for f in options if f['id'] == life['id'])['enabled'] = True
            with self.assertRaises(ValueError):
                E.evaluate(TEXT, league='Covens', manual_filters=options)

    def test_hybrid_ranges_apply_to_primary_and_intersect_duplicates(self):
        def lines(t, *args, **kwargs):
            return [{'id': 's.life', 'value': {'min': t['min']}}, {'id': 's.secondary'}], []

        kept = [{'type': 'mod', 'kind': 'explicit', 'target': {'value': 100},
                 'bounds': bounds} for bounds in ({'min': 70, 'max': 90}, {'min': 80, 'max': 85})]
        with patch.object(E, 'target_lines', side_effect=lines):
            payload = E._query('Crusader Boots', kept, {}, {}, {}, 'online', [])
            filters = payload['query']['stats'][0]['filters']
            self.assertEqual(filters, [{'id': 's.life', 'value': {'min': 80, 'max': 85}}, {'id': 's.secondary'}])
            kept[1]['bounds'] = {'min': 95, 'max': 100}
            with self.assertRaises(ValueError):
                E._query('Crusader Boots', kept, {}, {}, {}, 'online', [])

    def concurrent(self, identical):
        _, options, life = self.prepare()
        other = copy.deepcopy(options)
        if not identical:
            other[life]['min'] += 1
        entered, joined, release = threading.Event(), threading.Event(), threading.Event()
        barrier = threading.Barrier(2)
        count = trade.count_request

        def record(event):
            if event == 'evaluation.shared_wait':
                joined.set()
            count(event)

        def api(req, **kwargs):
            if '/search/' in req.full_url:
                entered.set()
                if identical:
                    if not release.wait(5):
                        raise AssertionError('Unreleased search')
                else:
                    barrier.wait(timeout=5)
            return self.fixture.api(req, **kwargs)

        with patch.object(trade, 'count_request', side_effect=record), \
                patch.object(trade.urllib.request, 'urlopen', side_effect=api), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(E.evaluate, TEXT, league='Covens', manual_filters=options)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(E.evaluate, TEXT, league='Covens', manual_filters=other)
                if identical:
                    self.assertTrue(joined.wait(5))
            finally:
                release.set()
            first.result(timeout=10)
            second.result(timeout=10)
        self.assertFalse(E._INFLIGHT)

    def test_identical_manual_requests_share_one_search(self):
        self.concurrent(True)
        self.assertEqual(self.fixture.sent['search'], 1)
        self.assertEqual(self.fixture.sent['fetch'], 2)

    def test_different_manual_ranges_are_not_combined(self):
        self.concurrent(False)
        self.assertEqual(self.fixture.sent['search'], 2)

    def test_manual_api_and_localized_validation(self):
        from poecraft import server
        _, options, _ = self.prepare()
        httpd = server.BoundedServer(('127.0.0.1', 0), server.Handler, 8)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            for language in ('en', 'ru', 'de'):
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=20)
                try:
                    conn.request('POST', '/api/evaluate', json.dumps({'text': TEXT, 'league': 'Covens',
                        'manual_filters': options, 'lang': language}), {'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200, result)
                    self.assertTrue(result['manual_filters'])
                    self.assertTrue(result['filters'][0]['reason'])
                finally:
                    conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)
