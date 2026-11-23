import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from poecraft import buildassistant as B, buildtree as T


def tree_source(name='Life'):
    nodes = ','.join(f'[{i}]={{["group"]=1,["name"]="{name}",["orbit"]=1,["orbitIndex"]={i % 6},["stats"]={{"+10 to Strength"}},["out"]={{"{i+1}"}}}}' for i in range(1, 121))
    return ('return {["constants"]={["orbitRadii"]={0,82},["skillsPerOrbit"]={1,6}},'
            '["groups"]={[1]={["x"]=100,["y"]=200}},["nodes"]={'+nodes+'}}').encode()


class BuildTree(unittest.TestCase):
    def test_node_list_positions_are_not_passive_ids_or_connections(self):
        data=T.lua_table(tree_source().decode())
        for i,node in enumerate(data['nodes'],1001):
            node.update(skill=i,out=[str(i+1)] if i<1120 else [])
        converted=T.convert(data)
        self.assertNotIn('1',converted['nodes'])
        self.assertIn('1001',converted['nodes'])
        self.assertIn(['1001','1002'],converted['links'])

    def test_pob_does_not_draw_virtual_ascendancy_start_or_mastery_connections(self):
        data=T.lua_table(tree_source().decode())
        # PoB PassiveTree.lua's connector contract, distinct from allocation adjacency.
        data['nodes'][0].update(out=['2','3','4','5'])
        data['nodes'][1]['ascendancyName']='Ascendant'
        data['nodes'][2]['classStartIndex']=0
        data['nodes'][3]['isMastery']=True
        converted=T.convert(data)
        for pair in [['1','2'],['1','3'],['1','4']]:
            self.assertNotIn(pair,converted['links'])
        self.assertIn(['1','5'],converted['links'])

    def test_orbit_geometry_retains_reference_group_and_arc_radius(self):
        data=T.lua_table(tree_source().decode())
        data['constants']={'orbitRadii':[0,82,162],'skillsPerOrbit':[1,6,16]}
        data['nodes'][0].update(orbit=2,orbitIndex=2)
        node=T.convert(data)['nodes']['1']
        self.assertAlmostEqual(node['x'],100+162*2**-.5,places=2)
        self.assertAlmostEqual(node['y'],200-162*2**-.5,places=2)
        self.assertEqual(node['group'],'1')
        self.assertEqual(node['radius'],162)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(T.config, 'DATA_DIR', Path(self.tmp.name));p.start();self.addCleanup(p.stop)

    def test_versioned_source_checksum_cache_and_license(self):
        raw = tree_source()
        with patch.object(T, '_download', side_effect=[json.dumps([{'sha':'a'*40}]).encode(), raw, b'MIT License']) as get:
            first = T.load_tree('3_30')
            self.assertEqual(first['sha256'], hashlib.sha256(raw).hexdigest())
            self.assertIn('/'+'a'*40+'/src/TreeData/3_30/tree.lua',first['source'])
            self.assertEqual(len(first['nodes']),120)
            self.assertAlmostEqual(first['nodes']['1']['x'],171.01,places=2)
            self.assertEqual(first,T.load_tree('3.30'))
            self.assertEqual(get.call_count,3)
        self.assertIn('MIT License',(Path(self.tmp.name)/'build_trees/NOTICE.txt').read_text())

    def test_other_version_never_reuses_current_tree(self):
        with patch.object(T, '_download', side_effect=[b'[{"sha":"'+b'a'*40+b'"}]',tree_source(),b'MIT']):T.load_tree('3_29')
        with patch.object(T, '_download', side_effect=T.TreeDataError('missing')):
            with self.assertRaisesRegex(T.TreeDataError,'missing'):T.load_tree('3_30')
        with patch.object(T,'_download',side_effect=AssertionError('Offline cache should work')):
            self.assertEqual(T.load_tree('3_29')['version'],'3_29')

    def test_unsafe_or_unknown_version(self):
        with patch.object(T,'_download') as get:
            for version in [None,'../3_30','https://evil','3_30/../../x','3_30_ruthless_typo']:
                with self.subTest(version=version), self.assertRaises(T.TreeDataError):T.load_tree(version)
            get.assert_not_called()

    def test_corrupt_source_is_not_cached(self):
        with patch.object(T,'_download',side_effect=[b'[{"sha":"'+b'a'*40+b'"}]',b'return {broken}']):
            with self.assertRaises(T.TreeDataError):T.load_tree('3_30')
        self.assertFalse((Path(self.tmp.name)/'build_trees/3_30.json').exists())

    def test_mastery_ascendancy_and_cluster_proxy(self):
        data=T.lua_table(tree_source().decode());node=data['nodes'][0]
        node.update(isMastery=True,masteryEffects=[{'effect':10,'stats':['+50 to Life']}],ascendancyName='Juggernaut')
        data['nodes'][1]['isProxy']=True
        converted=T.convert(data)
        self.assertEqual(converted['nodes']['1']['kind'],'mastery')
        self.assertEqual(converted['nodes']['1']['mastery_effects'][0]['effect'],10)
        self.assertEqual(converted['nodes']['1']['ascendancy'],'Juggernaut')
        self.assertNotIn('2',converted['nodes'])
        self.assertNotIn(['1','2'],converted['links'])

    def test_import_preserves_choices_and_overrides(self):
        xml='<PathOfBuilding><Build className="Witch"/><Tree><Spec treeVersion="3_30_ruthless" nodes="1,2,70000" masteryEffects="{1,10}"><Overrides><Override nodeId="2" dn="Tattoo"/></Overrides></Spec></Tree></PathOfBuilding>'
        spec=B.import_build(xml)['trees'][0]
        self.assertEqual(spec['nodes'],[1,2,70000])
        self.assertEqual(spec['mastery_effects'],'{1,10}')
        self.assertEqual(spec['overrides'],[{'nodeId':'2','dn':'Tattoo'}])

    def test_lua_mixed_groups_keep_implicit_and_explicit_keys(self):
        self.assertEqual(T.lua_table('return {[500]="extra", "first", "second"}'),
                         {500:'extra',1:'first',2:'second'})
        self.assertEqual(T.lua_table('return {"first", [1]="override"}'),['override'])
