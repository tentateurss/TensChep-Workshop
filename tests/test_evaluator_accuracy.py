import unittest
from unittest.mock import patch

from poecraft.engine import evaluator as E


class UniqueProperties(unittest.TestCase):
    def properties(self, lines, index=None):
        text = 'Item Class: Rings\nRarity: Unique\nExample Unique\nGold Ring\n--------\n'+lines
        with patch.object(E.trade, 'stat_index', return_value=index or {}):
            return E._unique_properties(E._header(text), False)

    def test_positive_negative_rolls_use_close_bands(self):
        index = {'explicit': {'+# to maximum Life':'explicit.life', '+#% to Fire Resistance':'explicit.fire'}}
        filters,_,_,missing = self.properties('+80 to maximum Life\n-30% to Fire Resistance', index)
        self.assertFalse(missing)
        self.assertEqual(filters, [{'id':'explicit.life','value':{'min':72,'max':88}},
                                   {'id':'explicit.fire','value':{'min':-33,'max':-27}}])

    def test_discrete_plus_one_is_not_relaxed_to_zero(self):
        filters,_,_,_ = self.properties('+1 to Level of all Spell Skill Gems',
                                       {'explicit':{'+# to Level of all Spell Skill Gems':'explicit.level'}})
        self.assertEqual(filters[0]['value'], {'min':1,'max':1})

    def test_aura_variants_require_the_matching_property(self):
        line = 'Unaffected by Bleeding while affected by Malevolence'
        filters,_,_,missing = self.properties(line, {'explicit':{line:'explicit.aura_variant'}})
        self.assertEqual(filters, [{'id':'explicit.aura_variant'}])
        self.assertFalse(missing)
        filters,_,_,missing = self.properties('Unaffected by Shock while affected by Purity of Lightning',
                                             {'explicit':{line:'explicit.aura_variant'}})
        self.assertFalse(filters)
        self.assertTrue(missing)

    def test_multi_number_and_ambiguous_local_stats_are_not_guessed(self):
        index={'explicit':{'Adds # to # Physical Damage':'explicit.adds',
                           '#% increased Physical Damage':'explicit.global',
                           '#% increased Physical Damage (Local)':'explicit.local'}}
        filters,_,_,missing = self.properties('Adds 10 to 20 Physical Damage\n100% increased Physical Damage', index)
        self.assertFalse(filters)
        self.assertEqual(len(missing),2)

    def test_six_links_are_not_compared_to_unlinked_six_sockets(self):
        _,sockets,_,missing=self.properties('Sockets: W-W-W-W-W-W')
        self.assertEqual(sockets,{'sockets':{'min':6,'max':6},'links':{'min':6}})
        self.assertFalse(missing)
        _,sockets,_,missing=self.properties('Sockets: R-G-B R-G-B')
        self.assertEqual(sockets['links'],{'min':3})
        _,sockets,_,missing=self.properties('Sockets: R-G-')
        self.assertFalse(sockets)
        self.assertTrue(missing)


class ComparableReliability(unittest.TestCase):
    def summary(self, prices, **properties):
        out={'kind':'rare','listings':[{'online':True,'chaos':p,'price':{'amount':p,'currency':'chaos'}} for p in prices],
             'passes':[{'status':'online','total':len(prices),'dropped':[]}],**properties}
        return E._summary(out)

    def test_small_and_widely_disparate_samples_are_not_valuations(self):
        for values,reason in [([10],'small_sample'),([10,11],'small_sample'),([1,10,100],'wide_range')]:
            result=self.summary(values)
            self.assertFalse(result['reliable_estimate'])
            self.assertEqual(result['reliability_reason'],reason)
        self.assertTrue(self.summary([10,11,12])['reliable_estimate'])
        result=self.summary([10,11,12],passes=[{'status':'online','total':3,'dropped':['minor modifier']}])
        self.assertFalse(result['reliable_estimate'])
        self.assertEqual(result['reliability_reason'],'relaxed')

    def test_unmapped_core_and_category_prices_are_not_valuations(self):
        result=self.summary([1,2,3],unmatched_properties=['valuable core mod'])
        self.assertFalse(result['reliable_estimate'])
        self.assertEqual(result['reliability_reason'],'unmatched')
        self.assertFalse(self.summary([10,11,12],kind='unique',comparison_scope='category')['reliable_estimate'])

    def test_unique_query_keeps_recognized_stats_sockets_and_status(self):
        captured=[]
        text="Item Class: Body Armours\nRarity: Unique\nTabula Rasa\nSimple Robe\n--------\nSockets: W-W-W-W-W-W\nCorrupted"
        out={'warnings':[],'passes':[],'_status':{'corrupted':{'option':'true'}}}
        def collect(out,league,make,note):
            payload=E._with_status(make('online'),out['_status'])
            captured.append(payload)
        with patch.object(E.trade,'stat_index',return_value={}),patch.object(E,'_with_offline_fallback',side_effect=collect):
            E._unique(out,E._header(text),('Simple Robe','Body Armour','test-base'),{'league':'Covens'})
        q=captured[0]['query']
        self.assertEqual(q['name'],'Tabula Rasa')
        self.assertEqual(q['filters']['socket_filters']['filters']['links']['min'],6)
        self.assertEqual(q['filters']['misc_filters']['filters']['corrupted']['option'],'true')

    def test_fixed_colours_and_foil_variants_do_not_inherit_an_ordinary_valuation(self):
        for name,rarity in [('Skin of the Lords','Unique'),('Tabula Rasa','Unique (Foil)')]:
            with self.subTest(name=name):
                out={'warnings':[],'passes':[],'_status':{}}
                text=f'Item Class: Body Armours\nRarity: {rarity}\n{name}\nSimple Robe\n--------\nSockets: R-G-B-R-G-B'
                def collect(out,league,make,note):
                    make('online')
                    note['total']=3
                    out['listings']=[{'online':True,'chaos':v,'price':{'amount':v,'currency':'chaos'}} for v in [10,11,12]]
                with patch.object(E.trade,'stat_index',return_value={}),patch.object(E,'_with_offline_fallback',side_effect=collect):
                    E._unique(out,E._header(text),('Simple Robe','Body Armour','test-base'),{'league':'Covens'})
                self.assertTrue(out['unmatched_properties'])
                self.assertFalse(E._summary(out)['reliable_estimate'])
