import unittest
from unittest.mock import patch
from poecraft import buildtrade as B


class BuildTrade(unittest.TestCase):
    def test_existing_trade_transport_receives_selected_league_and_reviewed_query(self):
        item={'text':'Rarity: UNIQUE\nTabula Rasa\nSimple Robe\nSockets: W-W-W-W-W-W'}
        preview={'query':B.trade.build_query('Simple Robe',[],'unique'),'rarity':'unique','filters':[],'language':'en','skipped':[]}
        with patch.object(B,'prepare',return_value=preview), patch.object(B.trade,'search_ids',return_value={'id':'realid','total':3}) as search:
            self.assertTrue(B.search(item,'Covens')['url'].endswith('/realid'))
            self.assertEqual(search.call_args.args[0],'Covens')
            self.assertEqual(search.call_args.args[1],B.payload(preview))

    def test_unresolved_variant_sends_no_requests(self):
        with patch.object(B.evaluator,'evaluate') as evaluate:
            with self.assertRaises(B.BuildTradeError):B.search({'text':'Rarity: UNIQUE\nName\nBase\n{variant:1}Effect'},'Covens')
            evaluate.assert_not_called()

    def test_never_returns_arbitrary_url(self):
        preview={'query':B.trade.build_query('Coral Ring',[],'normal'),'rarity':'normal','filters':[],'language':'en','skipped':[]}
        with patch.object(B,'prepare',return_value=preview), patch.object(B.trade,'search_ids',return_value={'id':'q1','total':1}), patch.object(B.trade,'search_url',return_value='https://evil.example/'):
            with self.assertRaises(B.BuildTradeError):B.search({'text':'Rarity: NORMAL\nCoral Ring'},'Covens')

    def test_rate_limit_does_not_open_previous_search(self):
        preview={'query':B.trade.build_query('Coral Ring',[],'normal'),'rarity':'normal','filters':[],'language':'en','skipped':[]}
        with patch.object(B,'prepare',return_value=preview), patch.object(B.trade,'search_ids',side_effect=B.trade.TradeError('Retry after 60 seconds')) as search:
            with self.assertRaises(B.trade.TradeError):B.search({'text':'Rarity: NORMAL\nCoral Ring'},'Covens')
            search.assert_called_once()
            self.assertFalse(B._flights)

    def test_literal_legacy_rolls_and_implicit_markers(self):
        text=B.item_text({'text':'Rarity: RARE\nName\nCoral Ring\nImplicits: 1\n+25 to maximum Life\n{crafted}+40 to maximum Life','mod_ranges':[{'id':'1','range':'0.5'}]})
        self.assertIn('+25 to maximum Life (implicit)',text)
        self.assertIn('+40 to maximum Life (crafted)',text)

    def test_affix_blueprint_does_not_replace_resolved_values(self):
        text=B.item_text({'text':'Rarity: RARE\nDoom Stride\nCrusader Boots\nPrefix: {range:0.5}IncreasedLife\nImplicits: 0\n+80 to maximum Life'})
        self.assertIn('+80 to maximum Life',text)
        self.assertNotIn('IncreasedLife',text)

    def test_localized_unique_name_is_preserved(self):
        from poecraft.buildassistant import import_build
        for rarity,name,base in [('Редкость: Уникальный','Знак Каома','Кольцо с кораллом'),
                                  ('Seltenheit: Einzigartig','Magierblut','Robuster Gürtel')]:
            raw=rarity+'\n'+name+'\n'+base
            build=import_build('<PathOfBuilding><Build/><Items><Item id="1">'+raw+'</Item></Items></PathOfBuilding>')
            self.assertEqual(build['inventory'][0]['name'],name)
            self.assertIn(name,B.item_text(build['inventory'][0]))

    def test_ranged_roll_is_not_guessed(self):
        with patch.object(B.evaluator,'evaluate') as evaluate:
            with self.assertRaises(B.BuildTradeError):B.search({'text':'Rarity: UNIQUE\nName\nBase\n(10-20)% increased Damage'},'Covens')
            evaluate.assert_not_called()

    def test_normalized_items_use_existing_unique_and_rare_queries(self):
        from test_engine import ItemEvaluator
        for text,kind,base in [
            ('Rarity: MAGIC\nHealthy Crusader Boots\n+80 to maximum Life','rare','Crusader Boots'),
            ('Rarity: RARE\nDoom Stride\nCrusader Boots\nItem Level: 84\nImplicits: 0\n+80 to maximum Life\n25% increased Movement Speed','rare','Crusader Boots'),
            ('Rarity: UNIQUE\nTabula Rasa\nSimple Robe\nSockets: W-W-W-W-W-W','unique','Simple Robe')]:
            with self.subTest(kind=kind):
                fixture=ItemEvaluator();fixture.setUp()
                result=fixture.run_eval(B.item_text({'text':text}),found=lambda payload,n:3)
                self.assertEqual(result['kind'],kind)
                self.assertEqual(fixture.searches[0]['query']['type'],base)
                if kind=='unique':self.assertEqual(fixture.searches[0]['query']['name'],'Tabula Rasa')
                else:self.assertTrue(fixture.filters(fixture.searches[0]))
