import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from poecraft import config, i18n
from poecraft.data import trade
from poecraft.engine import evaluator as E
from poecraft.engine.prices import Prices
from poecraft.messages import render

FIXTURES = Path(__file__).parent / 'fixtures'


class LocalizedUnique(unittest.TestCase):
    def setUp(self):
        E._CACHE.clear()
        trade._reset_limits()
        self.text = (FIXTURES/'kaoms_sign_ru.txt').read_text(encoding='utf-8')
        self.stats = json.loads((FIXTURES/'kaoms_sign_ru_stats.json').read_text(encoding='utf-8'))

    def test_exact_russian_unique_and_advanced_properties(self):
        requests = []
        def search(league, payload, wait=20, language='en'):
            requests.append((league, payload, language))
            return {'id':'exact', 'total':3, 'result':['a','b','c']}
        rows = [{'item':{'name':"Kaom's Sign",'typeLine':'Coral Ring'},
                 'listing':{'price':{'amount':1,'currency':'alch'},
                            'account':{'online':{'league':'Covens'}}}} for _ in range(3)]
        with patch.object(trade,'current_league',return_value={'league':'Covens','known':['Covens'],'source':'user'}), \
             patch.object(trade,'stat_index',return_value=self.stats) as stats, \
             patch.object(trade,'search_ids',side_effect=search), patch.object(trade,'fetch_listings',return_value=rows):
            result = render(E.evaluate(self.text,league='Covens'),'ru')
        league,payload,language = requests[0]
        self.assertEqual((league,language),('Covens','ru'))
        self.assertEqual(payload['query']['name'],self.text.splitlines()[2])
        self.assertEqual(payload['query']['type'],self.text.splitlines()[3])
        filters={f['id']:f for f in payload['query']['stats'][0]['filters']}
        self.assertEqual(set(filters),set(v for group in self.stats.values() for v in group.values()))
        self.assertNotIn('explicit.stat_3299347043',filters)  # base life is implicit, never an explicit-life demand
        self.assertEqual(filters['explicit.stat_1515657623']['value'],{'min':1,'max':1})
        self.assertEqual(result['unmatched_properties'],[])
        self.assertEqual(result['comparison_scope'],'properties')
        self.assertEqual(len(requests),1)
        self.assertIn('ru.pathofexile.com',result['search_url'])
        stats.assert_called_once_with('ru')

    def test_german_ascii_unique_keeps_its_query_language(self):
        text = "Gegenstandsklasse: Ringe\nSeltenheit: Einzigartig\nKaoms Zeichen\nKorallenring\n--------"
        payloads=[]
        def collect(out,lg,make,p):
            payloads.append(make('online'))
        with patch.object(trade,'stat_index',return_value={}),patch.object(E,'_with_offline_fallback',side_effect=collect):
            result=E._unique({'warnings':[],'passes':[]},E._header(text),None,{'league':'Covens'})
        self.assertEqual(result['query_language'],'de')
        self.assertEqual(payloads[0]['query']['name'],'Kaoms Zeichen')

    def test_unknown_conversion_is_not_a_fake_one_chaos_rate(self):
        prices=Prices(use_live=False)
        prices.table={}
        self.assertIsNone(E._price_chaos(prices,{'amount':1,'currency':'alch'}))
        self.assertEqual(E._price_chaos(prices,{'amount':1,'currency':'chaos'}),1)

    def test_small_positive_conversion_is_not_rounded_to_zero(self):
        prices=Prices(use_live=False)
        prices.live={'Orb of Alchemy':0.00003}
        self.assertEqual(E._price_chaos(prices,{'amount':1,'currency':'alch'}),0.00003)
        self.assertIsNone(E._price_chaos(prices,{'amount':0,'currency':'alch'}))

    def test_localized_stat_cache_is_separate(self):
        with tempfile.TemporaryDirectory() as d,patch.object(config,'DATA_DIR',Path(d)),patch.object(trade.urllib.request,'urlopen') as net:
            for language,text in [('en','english'),('ru','russian')]:
                suffix='' if language=='en' else '_'+language
                body={'result':[{'id':'explicit','entries':[{'id':'stat','text':text}]}]}
                (Path(d)/('trade_stats'+suffix+'.json')).write_text(json.dumps({'fetched':time.time(),'body':body}))
            self.assertIn('english',trade.stat_index()['explicit'])
            self.assertIn('russian',trade.stat_index('ru')['explicit'])
            net.assert_not_called()

    def test_query_cache_and_zero_pruning_do_not_cross_languages(self):
        payload={'query':{'type':'Example','stats':[]}}
        urls=[]
        def send(kind,request,wait):
            urls.append(request.full_url)
            return {'id':'zero','total':0,'result':[]}
        with patch.object(trade,'_ggg',side_effect=send):
            trade.search_ids('Covens',payload)
            trade.search_ids('Covens',payload,language='ru')
            trade.search_ids('Covens',payload,language='ru')
        self.assertEqual(len(urls),2)
        self.assertTrue(urls[0].startswith('https://www.pathofexile.com/'))
        self.assertTrue(urls[1].startswith('https://ru.pathofexile.com/'))
        with self.assertRaises(ValueError):
            trade.search_ids('Covens',payload,language='https://invalid.example')
