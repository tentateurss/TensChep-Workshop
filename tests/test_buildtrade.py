import unittest
from unittest.mock import patch
from poecraft import buildtrade as B


class BuildTrade(unittest.TestCase):
    def test_existing_evaluator_receives_selected_league(self):
        item={'text':'Rarity: UNIQUE\nTabula Rasa\nSimple Robe\nSockets: W-W-W-W-W-W'}
        with patch.object(B.evaluator,'evaluate',return_value={'search_url':'https://www.pathofexile.com/trade/search/Covens/realid'}) as evaluate:
            self.assertTrue(B.search(item,'Covens')['url'].endswith('/realid'))
            self.assertEqual(evaluate.call_args.kwargs,{'league':'Covens','load_listings':True})
            self.assertIn('--------',evaluate.call_args.args[0])

    def test_unresolved_variant_sends_no_requests(self):
        with patch.object(B.evaluator,'evaluate') as evaluate:
            with self.assertRaises(B.BuildTradeError):B.search({'text':'Rarity: UNIQUE\nName\nBase\n{variant:1}Effect'},'Covens')
            evaluate.assert_not_called()

    def test_never_returns_arbitrary_url(self):
        with patch.object(B.evaluator,'evaluate',return_value={'search_url':'https://evil.example/'}):
            with self.assertRaises(B.BuildTradeError):B.search({'text':'Rarity: NORMAL\nCoral Ring'},'Covens')

    def test_rate_limit_does_not_open_previous_search(self):
        notes=[{'level':'error','text':'Retry after 60 seconds'}]
        with patch.object(B.evaluator,'evaluate',return_value={'search_url':'https://www.pathofexile.com/trade/search/Covens/old','notes':notes}) as evaluate:
            self.assertIsNone(B.search({'text':'Rarity: NORMAL\nCoral Ring'},'Covens')['url'])
            evaluate.assert_called_once()

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
