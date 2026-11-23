import unittest
from unittest.mock import patch
from poecraft import buildtrade as B


class BuildTradeReview(unittest.TestCase):
    def test_local_and_global_mods_are_not_silently_interchanged(self):
        item={'text':'Rarity: RARE\nCorruption Paw\nLeviathan Gauntlets\n100% increased Armour'}
        index={'explicit':{'#% increased Armour':'explicit.global_armour',
                           '#% increased Armour (Local)':'explicit.local_armour'}}
        with patch.object(B.evaluator,'_all_names',return_value={'leviathan gauntlets':('Leviathan Gauntlets','Gloves','base')}):
            p=B.prepare(item,index=index)
        f=p['filters'][0]
        self.assertFalse(f['enabled'])
        self.assertEqual(f['reason'],'build.trade.ambiguous')
        with self.assertRaisesRegex(B.BuildTradeError,'no_filters'):B.payload(p)
        rows=[{k:f[k] for k in ('id','enabled','choice','min','max')}]
        rows[0].update(enabled=True,choice='explicit.local_armour')
        self.assertEqual(B.payload(p,rows)['query']['stats'][0]['filters'][0]['id'],'explicit.local_armour')

    def test_socket_filters_are_visible_editable_and_not_hidden_constraints(self):
        item={'text':'Rarity: UNIQUE\nThe Dark Seer\nShadow Sceptre\nSockets: B-B-B'}
        with patch.object(B.evaluator,'_all_names',return_value={'shadow sceptre':('Shadow Sceptre','Sceptre','base')}):
            p=B.prepare(item,index={})
        self.assertEqual(len(p['filters']),2)
        q=B.payload(p)
        self.assertEqual(q['query']['filters']['socket_filters']['filters']['links'],{'min':3})
        rows=[{k:f[k] for k in ('id','enabled','choice','min','max')} for f in p['filters']]
        for row in rows:row['enabled']=False
        self.assertNotIn('socket_filters',B.payload(p,rows)['query']['filters'])
        rows[0].update(enabled=True,min=2.5)
        with self.assertRaises(B.BuildTradeError):B.payload(p,rows)

    def test_pseudo_review_does_not_change_evaluators_default_stat_catalogue(self):
        body={'result':[{'id':'pseudo','entries':[{'id':'pseudo.pseudo_total_life','text':'# total Life'}]}]}
        with patch.object(B.trade,'_get',return_value=body) as network:
            self.assertNotIn('pseudo',B.trade.stat_index())
            self.assertIn('pseudo.pseudo_total_life',B.trade.stat_index(body=body)['pseudo'].values())
            network.assert_called_once()

    def test_cached_zero_has_valid_official_payload_link_and_invalid_response_is_not_no_matches(self):
        p={'query':B.trade.build_query('Shadow Sceptre',[],'unique'),'rarity':'unique',
           'filters':[],'language':'en','skipped':[]}
        with patch.object(B,'prepare',return_value=p),patch.object(B.trade,'search_ids',return_value={'id':None,'total':0,'inferred':True}):
            for language in ('en','ru','de'):
                p['language']=language
                result=B.search({},'Covens',[])
                self.assertTrue(result['inferred'])
                self.assertTrue(result['url'].startswith(B.trade._site(language)+'/trade/search/Covens?q='))
        with patch.object(B,'prepare',return_value=p),patch.object(B.trade,'search_ids',return_value={'id':None,'total':0}):
            with self.assertRaisesRegex(B.BuildTradeError,'unavailable'):B.search({},'Covens',[])

    def test_corrupt_cached_catalogue_does_not_start_a_network_refresh(self):
        import tempfile,json
        from pathlib import Path
        item={'text':'Rarity: UNIQUE\nThe Dark Seer\nShadow Sceptre'}
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'stats.json';path.write_text(json.dumps({'body':None}))
            with patch.object(B.evaluator,'_all_names',return_value={'shadow sceptre':('Shadow Sceptre','Sceptre','base')}),patch.object(B.trade,'_cache',return_value=path),patch.object(B.trade,'_get') as network:
                with self.assertRaisesRegex(B.BuildTradeError,'catalogue'):B.prepare(item)
                network.assert_not_called()

    def test_german_magic_header_does_not_consume_first_modifier(self):
        text=B.item_text({'text':'Seltenheit: Magisch\nHealthy Coral Ring\n+80 to maximum Life'})
        self.assertIn('Healthy Coral Ring\n--------\n+80 to maximum Life',text)

    def test_rare_life_chaos_armour_and_explicit_pseudo_selection(self):
        item={'text':'Rarity: RARE\nCorruption Paw\nLeviathan Gauntlets\nArmour: 1000\nItem Level: 86\n+140 to maximum Life\n+27% to Chaos Resistance'}
        index={'explicit':{'+# to maximum Life':'explicit.stat_3299347043',
                           '+#% to Chaos Resistance':'explicit.stat_2923486259'},
               'pseudo':{'+# total maximum Life':'pseudo.pseudo_total_life',
                         '+#% total to Chaos Resistance':'pseudo.pseudo_total_chaos_resistance'}}
        with patch.object(B.evaluator,'_all_names',return_value={'leviathan gauntlets':('Leviathan Gauntlets','Gloves','base')}):
            p=B.prepare(item,index=index)
        settings=[{k:f[k] for k in ('id','enabled','choice','min','max')} for f in p['filters']]
        for row in settings:
            if row['choice']=='explicit.stat_3299347043':row.update(choice='pseudo.pseudo_total_life',min=110)
        query=B.payload(p,settings)['query']
        defence=next(f for f in p['filters'] if f['kind']=='defence')
        self.assertEqual(defence['reason'],'build.trade.defence.normalized')
        self.assertEqual(query['filters']['armour_filters']['filters']['ar'],{'min':800})
        self.assertIn({'id':'pseudo.pseudo_total_life','value':{'min':110}},query['stats'][0]['filters'])
        self.assertIn({'id':'explicit.stat_2923486259','value':{'min':21.6}},query['stats'][0]['filters'])

    def test_changed_review_or_forged_stat_is_rejected_before_network(self):
        p={'query':B.trade.build_query('Shadow Sceptre',[],'unique'),'rarity':'unique',
           'filters':[],'language':'en','skipped':[]}
        with patch.object(B,'prepare',return_value=p),patch.object(B.trade,'search_ids') as network:
            with self.assertRaisesRegex(B.BuildTradeError,'changed'):
                B.search({},'Covens',[],expected={'query':{'type':'Other Base'}})
            network.assert_not_called()

    def test_zero_online_does_not_trigger_hidden_offline_or_broad_search(self):
        p={'query':B.trade.build_query('Shadow Sceptre',[],'unique'),'rarity':'unique',
           'filters':[],'language':'en','skipped':[]}
        p['query']['query']['name']='The Dark Seer'
        with patch.object(B,'prepare',return_value=p),patch.object(B.trade,'search_ids',return_value={'id':'q1','total':0}) as network:
            result=B.search({},'Covens',[])
            self.assertEqual(result['state'],'no_online_matches')
            network.assert_called_once()
            self.assertEqual(network.call_args.args[1]['query']['status'],{'option':'online'})
        with patch.object(B,'prepare',return_value=p),patch.object(B.trade,'search_ids',return_value={'id':'q2','total':0}) as network:
            result=B.search({},'Covens',[],status='any')
            self.assertEqual(result['state'],'no_matches')
            network.assert_called_once()

    def test_parallel_reviewed_queries_share_success_and_error_without_hanging(self):
        from concurrent.futures import ThreadPoolExecutor, Future
        import threading
        p={'query':B.trade.build_query('Shadow Sceptre',[],'unique'),'rarity':'unique',
           'filters':[],'language':'en','skipped':[]}
        for fail in (False,True):
            ready=threading.Barrier(2);waiting=threading.Event()
            class WaitingFuture(Future):
                def result(self,*args,**kwargs):
                    waiting.set()
                    return super().result(*args,**kwargs)
            def prepare(*args):
                ready.wait(timeout=3)
                return p
            def search(*args,**kwargs):
                self.assertTrue(waiting.wait(timeout=3),'Second search must actually wait on the first')
                if fail:raise B.trade.TradeError('HTTP 429; Retry-After 60')
                return {'id':'q1','total':2}
            with patch.object(B,'Future',WaitingFuture),patch.object(B,'prepare',side_effect=prepare),patch.object(B.trade,'search_ids',side_effect=search) as network,ThreadPoolExecutor(max_workers=2) as pool:
                jobs=[pool.submit(B.search,{},'Covens',[]) for _ in range(2)]
                for job in jobs:
                    if fail:
                        with self.assertRaises(B.trade.TradeError):job.result(timeout=5)
                    else:self.assertEqual(job.result(timeout=5)['total'],2)
                network.assert_called_once()
            self.assertFalse(B._flights)

    def test_magic_flask_preserves_rarity_and_explicit_effects_without_network(self):
        item = {'text': "Rarity: MAGIC\nClinician's Quartz Flask of the Kakapo\nItem Level: 84\n33% increased Duration\n55% reduced Effect of Curses on you during Effect"}
        index = {'explicit': {'#% increased Duration': 'explicit.stat_1256719186',
                 '#% reduced Effect of Curses on you during Effect': 'explicit.stat_4265534424'}}
        with patch.object(B.evaluator, '_all_names', return_value={'quartz flask': ('Quartz Flask','UtilityFlask','flask')}), patch.object(B.trade, 'search_ids') as network:
            preview = B.prepare(item, index=index)
            query = B.payload(preview)['query']
            self.assertEqual(query['type'], 'Quartz Flask')
            self.assertEqual(query['filters']['type_filters']['filters']['rarity']['option'], 'magic')
            self.assertEqual(len(query['stats'][0]['filters']), 2)
            network.assert_not_called()

    def test_rare_cannot_silently_become_a_base_only_search(self):
        preview = {'query':B.trade.build_query('Leviathan Gauntlets', [], 'rare'),
                   'rarity':'rare', 'filters':[]}
        with self.assertRaisesRegex(B.BuildTradeError, 'no_filters'):
            B.payload(preview)

    def test_seller_mode_is_explicit_and_does_not_broaden_filters(self):
        preview = {'query':B.trade.build_query('Shadow Sceptre', [], 'unique'),
                   'rarity':'unique', 'filters':[]}
        preview['query']['query']['name'] = 'The Dark Seer'
        online = B.payload(preview, status='online')
        any_seller = B.payload(preview, status='any')
        self.assertEqual(online['query']['status']['option'], 'online')
        self.assertEqual(any_seller['query']['status']['option'], 'any')
        self.assertEqual(any_seller['query']['name'], 'The Dark Seer')
        self.assertEqual(online['query']['filters'], any_seller['query']['filters'])
