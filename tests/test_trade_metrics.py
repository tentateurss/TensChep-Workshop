import collections
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

from poecraft.data import trade
from poecraft.engine import evaluator as E, prices
import test_engine as fixtures


class TradeMetrics(unittest.TestCase):
    def setUp(self):
        E._CACHE.clear();trade._reset_limits()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.sent=collections.Counter();self.mode='found';self.barrier=None
        self.measurements=[]
        original=prices.Prices
        for p in [patch.object(trade.config,'DATA_DIR',Path(self.tmp.name)),
                  patch.object(trade.config,'TRADE_MAX_PER_10S',100),
                  patch.object(trade.config,'TRADE_MAX_SEARCH_PER_MIN',100),
                  patch.object(prices,'Prices',side_effect=lambda *a,**k:original(use_live=False)),
                  patch.object(trade.urllib.request,'urlopen',side_effect=self.api)]:
            p.start();self.addCleanup(p.stop)

    def api(self,req,**kwargs):
        url=req.full_url
        if '/data/leagues' in url:
            key='leagues';body={'result':[{'id':'Covens','realm':'pc'},{'id':'Standard','realm':'pc'}]}
        elif '/data/stats' in url:
            key='stats';body={'result':[{'id':kind,'entries':[{'text':text,'id':ident} for text,ident in entries.items()]}
                                      for kind,entries in fixtures.ItemEvaluator.STATS.items() if not kind.startswith('_')]}
        elif '/search/' in url:
            key='search';self.sent[key]+=1
            if self.mode=='429':raise urllib.error.HTTPError(url,429,'limited',{'Retry-After':'120'},None)
            if self.barrier:self.barrier.wait(timeout=10)
            number=self.sent[key];found=self.mode!='empty' and (self.mode!='relaxed' or number>=3)
            return self.response({'id':'query','total':15 if found else 0,'result':[f'{url.rsplit("/",1)[1]}-{i}' for i in range(15)] if found else []})
        elif '/fetch/' in url:
            key='fetch'
            if self.barrier:self.barrier.wait(timeout=10)
            body={'result':[{'item':{'name':'Sin Trek','typeLine':'Stealth Boots'},'listing':{
                'price':{'amount':2,'currency':'chaos'},'account':{'online':{'league':'Covens'}}}}]}
        else:raise AssertionError('Unexpected external endpoint')
        self.sent[key]+=1
        return self.response(body)

    def response(self,body):
        response=io.BytesIO(json.dumps(body).encode());response.headers={};return response

    def evaluate(self,text,league='Covens'):
        result=E.evaluate(text,league=league)
        self.assertTrue(all(isinstance(n,int) and n>=0 for n in result['trade_metrics'].values()))
        self.measurements.append({'kind':result.get('kind'),'counts':result['trade_metrics']})
        return result

    def test_cold_and_repeat_unique_currency_consumable_rare(self):
        cases={'unique':'Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------\nItem Level: 80',
               'currency':'Item Class: Stackable Currency\nRarity: Currency\nChaos Orb\n--------\nStack Size: 10/20',
               'consumable':'Item Class: Stackable Currency\nRarity: Currency\nDeafening Essence of Greed\n--------',
               'rare':fixtures.ItemEvaluator.BOOTS}
        for name,text in cases.items():
            with self.subTest(name=name):
                E._CACHE.clear();trade._reset_limits();self.sent.clear()
                for file in Path(self.tmp.name).glob('trade_*.json'):file.unlink()
                first=self.evaluate(text);a=dict(self.sent);second=self.evaluate(text)
                self.assertEqual(dict(self.sent),a)
                self.assertEqual(first['trade_metrics'].get('search.sent'),1)
                self.assertEqual(first['trade_metrics'].get('fetch.sent'),2)
                self.assertEqual(second['trade_metrics'].get('evaluation.cache_hit'),1)
                self.assertNotIn('search.sent',second['trade_metrics'])

    def parallel_requests(self):
        trade.leagues();trade.stat_index();self.sent.clear()
        text='Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------'
        entered, joined, release = threading.Event(), threading.Event(), threading.Event()
        count = trade.count_request

        def record(event):
            if event == 'evaluation.shared_wait':
                joined.set()
            count(event)

        def api(request, **kwargs):
            if '/search/' in request.full_url:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('Search was not released')
            return self.api(request, **kwargs)

        with patch.object(trade, 'count_request', side_effect=record), \
                patch.object(trade.urllib.request, 'urlopen', side_effect=api), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.evaluate, text)
            try:
                self.assertTrue(entered.wait(5))
                second = pool.submit(self.evaluate, text)
                self.assertTrue(joined.wait(5))
            finally:
                release.set()
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertFalse(E._INFLIGHT)
        return results

    def test_parallel_identical_evaluations_share_requests(self):
        results = self.parallel_requests()
        self.assertEqual(self.sent['search'],1);self.assertEqual(self.sent['fetch'],2)
        self.assertEqual(results[1]['trade_metrics']['evaluation.shared_wait'],1)
        self.assertNotIn('search.sent',results[1]['trade_metrics'])
        self.assertEqual(results[0]['summary'],results[1]['summary'])
        self.assertFalse(E._INFLIGHT)

    def test_parallel_429_is_shared_without_retrying(self):
        self.mode = '429'
        results = self.parallel_requests()
        self.assertEqual(self.sent['search'], 1)
        self.assertEqual(self.sent['fetch'], 0)
        self.assertEqual(results[0]['trade_metrics']['search.http_429'], 1)
        self.assertEqual(results[1]['trade_metrics']['evaluation.shared_wait'], 1)
        self.assertEqual(results[0]['summary'], results[1]['summary'])
        self.assertFalse(E._INFLIGHT)

    def test_429_and_retry_after_prevent_followup_request(self):
        self.mode='429'
        with patch.object(E,'_wait',return_value=0):
            first=self.evaluate('Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------')
            second=self.evaluate('Item Class: Body Armours\nRarity: Unique\nTabula Rasa\nSimple Robe\n--------')
        self.assertEqual(self.sent['search'],1)
        self.assertEqual(first['trade_metrics']['search.http_429'],1)
        self.assertEqual(second['trade_metrics']['search.slot_rejected'],1)

    def test_league_change_is_a_new_search_not_a_duplicate(self):
        text='Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------'
        self.evaluate(text);self.evaluate(text,'Standard');self.evaluate(text)
        self.assertEqual(self.sent['search'],2);self.assertEqual(self.sent['fetch'],4)

    def test_rare_relaxation_budget(self):
        self.mode='relaxed';result=self.evaluate(fixtures.ItemEvaluator.BOOTS)
        self.assertEqual(self.sent['search'],3);self.assertEqual(self.sent['fetch'],2)
        self.assertEqual(result['trade_metrics']['search.reason.rare_relaxed'],2)
        self.assertTrue(result['passes'][-1]['dropped'])

    def test_reference_and_transport_cache_events(self):
        @trade.measure_evaluation
        def inspect():
            trade.stat_index();trade.stat_index()
            payload={'query':{'type':'Secret private item'}}
            trade.search_ids('Covens',payload);trade.search_ids('Covens',payload)
            trade.fetch_listings('query',['one']);trade.fetch_listings('query',['one'])
            return {}
        result=inspect();metrics=result['trade_metrics']
        self.assertEqual(metrics['reference.stats.sent'],1)
        self.assertEqual(metrics['reference.stats.cache_hit'],1)
        self.assertEqual(metrics['search.cache_hit'],1);self.assertEqual(metrics['fetch.cache_hit'],1)
        self.assertNotIn('Secret',json.dumps(metrics));self.assertNotIn('Covens',json.dumps(metrics))

    def test_http_response_exposes_only_safe_counters_in_each_language(self):
        import http.client
        from poecraft import server

        httpd = server.BoundedServer(('127.0.0.1', 0), server.Handler, 8)
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            for language in ('en', 'ru', 'de'):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=20)
                try:
                    connection.request('POST', '/api/evaluate', json.dumps({
                        'text': 'Item Class: Boots\nRarity: Unique\nSin Trek\nStealth Boots\n--------',
                        'league': 'Covens', 'lang': language,
                    }), {'Content-Type': 'application/json'})
                    response = connection.getresponse()
                    result = json.loads(response.read())
                    self.assertEqual(response.status, 200, result)
                    self.assertEqual(result['kind'], 'unique')
                    self.assertIn('trade_metrics', result)
                    metrics = json.dumps(result['trade_metrics'])
                    self.assertNotIn('Sin Trek', metrics)
                    self.assertNotIn('Covens', metrics)
                    if language == 'en':
                        self.assertEqual(result['trade_metrics']['search.sent'], 1)
                    else:
                        self.assertEqual(result['trade_metrics']['evaluation.cache_hit'], 1)
                finally:
                    connection.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)
