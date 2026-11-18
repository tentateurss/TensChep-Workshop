import unittest

from poecraft import buildassistant as B
from test_buildassistant import MODERN


def entry(ident, title):
    return {'id': ident, 'title': title}


class AuthorLoadouts(unittest.TestCase):
    def test_explicit_identifiers_link_different_names_and_colours(self):
        stages, ambiguous = B._loadouts([entry('1', '^2Levelling tree {early}')],
            [entry('7', '^6Gems {early}')], [entry('3', 'Equipment {early}')], [])
        self.assertFalse(ambiguous)
        self.assertEqual(stages[0], {'id':'identifier:early','title':'^2Levelling tree {early}',
                         'tree':'1','skills':'7','items':'3','config':None,'association':'identifier'})

    def test_shared_item_and_config_sets_can_serve_multiple_stages(self):
        stages, _ = B._loadouts([entry('1','Tree {1}'),entry('2','Tree {2}')],
            [entry('1','Gems {1}'),entry('2','Gems {2}')],
            [entry('5','Items {1,2}'),entry('6','Other items {3}')], [entry('8','Default')])
        self.assertEqual([(s['tree'],s['skills'],s['items'],s['config']) for s in stages],
                         [('1','1','5','8'),('2','2','5','8')])

    def test_exact_names_preserve_colour_and_case_for_matching(self):
        trees=[entry('1','^2Maps'),entry('2','Other')]
        items=[entry('1','^2Maps'),entry('2','Items')]
        skills=[entry('1','^2Maps'),entry('2','Skills')]
        self.assertEqual(len(B._loadouts(trees,skills,items,[])[0]),1)
        for title in ['Maps','^3Maps','^2maps','^2Maps ']:
            with self.subTest(title=title):
                self.assertEqual(B._loadouts(trees,[entry('1',title),skills[1]],items,[])[0],[])

    def test_similar_titles_and_set_order_are_not_links(self):
        stages, _ = B._loadouts([entry('1','Act 1'),entry('2','Maps')],
            [entry('1','Levelling'),entry('2','Endgame')],
            [entry('1','Starter'),entry('2','Expensive')],[])
        self.assertEqual(stages,[])

    def test_duplicate_markers_or_tree_titles_do_not_choose_a_winner(self):
        stages, ambiguous = B._loadouts([entry('1','Tree {1}'),entry('2','Tree {2}')],
            [entry('1','Gems {1}'),entry('2','Duplicate {1}'),entry('3','Gems {2}')],
            [entry('1','Shared')],[])
        self.assertTrue(ambiguous)
        self.assertEqual([s['id'] for s in stages],['identifier:2'])
        stages, ambiguous = B._loadouts([entry('1','Maps'),entry('2','Maps')],
            [entry('1','Only gems')],[entry('1','Only items')],[])
        self.assertTrue(ambiguous)
        self.assertEqual(stages,[])

    def test_singleton_fallback_is_confirmed_except_for_missing_tree_title(self):
        stages, _ = B._loadouts([entry('1','Early'),entry('2','Late')],
            [entry('1',None)],[entry('7','Default')],[])
        self.assertEqual(len(stages),2)
        self.assertEqual(B._loadouts([entry('1',None)], [entry('1',None)],[entry('1',None)],[])[0],[])

    def test_identifiers_take_priority_over_names_and_use_no_partial_matching(self):
        stages, _ = B._loadouts([entry('1','Maps {a}')],
            [entry('1','Maps {b}'),entry('2','Maps {ab}')], [entry('1','Shared')],[])
        self.assertEqual(stages,[])

    def test_multiple_configs_must_also_match_but_are_not_executed(self):
        args=([entry('1','Maps')],[entry('1','Maps')],[entry('1','Maps')])
        self.assertEqual(B._loadouts(*args,[entry('1','Default'),entry('2','Boss')])[0],[])
        stages,_=B._loadouts(*args,[entry('1','Default'),entry('2','Maps')])
        self.assertEqual(stages[0]['config'],'2')

    def test_full_import_links_only_the_confirmed_variant(self):
        result=B.import_build(MODERN)
        self.assertEqual([(x['tree'],x['skills'],x['items']) for x in result['loadouts']],[('2','2','2')])
        unlinked=B.import_build(MODERN.replace('title="Maps"','title="Different"',1))
        self.assertEqual(unlinked['loadouts'],[])
        self.assertIn('separate_sets',unlinked['warnings'])

    def test_duplicate_config_ids_are_corrupt_not_confirmed_links(self):
        xml=MODERN.replace('</PathOfBuilding>','<Config><ConfigSet id="1"/><ConfigSet id="1"/></Config></PathOfBuilding>')
        with self.assertRaisesRegex(B.BuildImportError,'format'):B.import_build(xml)
