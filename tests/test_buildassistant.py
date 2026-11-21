import base64
import hashlib
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
import zlib

from poecraft import buildassistant as B

FIXTURES = Path(__file__).parent / 'fixtures' / 'pob'
MODERN = '''<PathOfBuilding><Build className="Witch" ascendClassName="Necromancer" level="95" targetVersion="3_0" mainSocketGroup="1"><PlayerStat stat="CombinedDPS" value="999999999"/></Build>
<Tree activeSpec="2"><Spec title="Act 1" treeVersion="3_30" nodes="1,2"/><Spec title="Maps" treeVersion="3_30" nodes="12,34,65537" masteryEffects="{12,500}"><URL>https://www.pathofexile.com/passive-skill-tree/AAAA</URL><Sockets><Socket nodeId="12" itemId="1"/></Sockets></Spec></Tree>
<Skills activeSkillSet="2"><SkillSet id="1" title="Levelling"><Skill><Gem nameSpec="Fireball" level="1"/></Skill></SkillSet><SkillSet id="2" title="Maps"><Skill slot="Body Armour" enabled="true" mainActiveSkill="1"><Gem nameSpec="Summon Raging Spirit" level="20" quality="20" enabled="true"/><Gem nameSpec="Minion Damage" level="20" quality="0" enabled="false"/></Skill></SkillSet></Skills>
<Items activeItemSet="2"><Item id="1" variant="2">Rarity: UNIQUE
Tabula Rasa
Simple Robe
Sockets: W-W-W-W-W-W<ModRange id="1" range="0.5"/></Item><ItemSet id="1" title="Starter"><Slot name="Body Armour" itemId="0"/></ItemSet><ItemSet id="2" title="Maps"><Slot name="Body Armour" itemId="1"/></ItemSet></Items></PathOfBuilding>'''


def encode(xml):
    return base64.urlsafe_b64encode(zlib.compress(xml.encode())).decode().rstrip('=')


class BuildImport(unittest.TestCase):
    def rejected(self, source, code):
        with self.assertRaises(B.BuildImportError) as error:
            B.import_build(source)
        self.assertEqual(str(error.exception), code)

    def test_real_berserker_xml(self):
        result = B.import_build((FIXTURES/'Dual_Savior.xml').read_text(encoding='utf-8'))
        self.assertEqual((result['class'], result['ascendancy'], result['level']), ('Marauder', 'Berserker', 1))
        self.assertEqual(result['trees'][0]['version'], '3_13')
        self.assertEqual(len(result['trees'][0]['nodes']), 8)
        self.assertEqual(len(result['skill_sets'][0]['groups']), 4)
        self.assertEqual(result['inventory'][0]['name'], 'The Saviour')
        self.assertEqual([s['id'] for s in result['item_sets'] if s['active']], ['1'])

    def test_real_occultist_code_matches_xml(self):
        xml = (FIXTURES/'OccVortex.xml').read_text(encoding='utf-8')
        result = B.import_build(encode(xml))
        self.assertEqual(result, B.import_build(xml))
        self.assertEqual((result['class'], result['ascendancy'], result['level']), ('Witch', 'Occultist', 99))
        self.assertEqual(len(result['trees'][0]['nodes']), 132)
        self.assertEqual(len(result['inventory']), 18)
        self.assertEqual(len(result['skill_sets'][0]['groups']), 6)

    def test_modern_sets_links_variants_and_no_invented_statistics(self):
        result = B.import_build(MODERN)
        self.assertEqual([t['title'] for t in result['trees']], ['Act 1', 'Maps'])
        self.assertEqual(result['trees'][1]['nodes'], [12, 34, 65537])
        self.assertEqual(result['trees'][1]['mastery_effects'], '{12,500}')
        self.assertEqual(result['trees'][1]['sockets'][0]['itemId'], '1')
        self.assertTrue(result['trees'][1]['active'])
        self.assertTrue(result['skill_sets'][1]['active'])
        gems = result['skill_sets'][1]['groups'][0]['gems']
        self.assertEqual([(g['name'], g['level'], g['quality'], g['enabled']) for g in gems],
                         [('Summon Raging Spirit',20,20,True), ('Minion Damage',20,0,False)])
        self.assertEqual(result['inventory'][0]['variants']['variant'], '2')
        self.assertEqual(result['inventory'][0]['mod_ranges'][0]['range'], '0.5')
        self.assertNotIn('item', result['item_sets'][1]['slots'][0])  # references, not duplicated item text
        self.assertNotIn('dps', result)
        self.assertNotIn('999999999', json.dumps(result))
        self.assertIn('linked_sets', result['warnings'])

    def test_missing_information_stays_missing(self):
        result = B.import_build('<PathOfBuilding><Build/></PathOfBuilding>')
        self.assertIsNone(result['level'])
        self.assertIsNone(result['class'])
        self.assertEqual(result['trees'], [])
        self.assertEqual(result['skill_sets'], [])
        self.assertEqual(result['item_sets'], [])

    def test_duplicate_set_identifiers_are_not_silently_aliased(self):
        for tag in ['SkillSet', 'ItemSet']:
            with self.subTest(tag=tag):
                self.rejected(MODERN.replace('<'+tag+' id="2"', '<'+tag+' id="1"'), 'format')

    def test_missing_nodes_distinguished_from_zero_allocated(self):
        result = B.import_build('<PathOfBuilding><Build/><Tree><Spec/><Spec nodes=""/></Tree></PathOfBuilding>')
        self.assertIsNone(result['trees'][0]['nodes'])
        self.assertEqual(result['trees'][1]['nodes'], [])

    def test_missing_inventory_reference_has_warning(self):
        result = B.import_build(MODERN.replace('itemId="1"/></ItemSet>', 'itemId="99"/></ItemSet>'))
        self.assertIn('missing_items', result['warnings'])

    def test_rejects_poe2_root_and_target(self):
        self.rejected(MODERN.replace('PathOfBuilding>', 'PathOfBuilding2>'), 'poe2')
        self.rejected(MODERN.replace('3_0', '0_1'), 'poe2')
        self.rejected(MODERN.replace('3_30', '0_4'), 'version')

    def test_rejects_nonbuild_unknown_version_and_bad_nodes(self):
        for source, code in [('<html/>','format'), ('<PathOfBuilding/>','format'),
                             (MODERN.replace('3_0','9_0'),'version'),(MODERN.replace('1,2','1,broken'),'format')]:
            with self.subTest(code=code): self.rejected(source, code)

    def test_rejects_dtd_and_entities(self):
        for declaration in ['<!DOCTYPE PathOfBuilding SYSTEM "file:///secret">',
                            '<!DOCTYPE PathOfBuilding [<!ENTITY x "expanded">]>']:
            self.rejected(declaration+MODERN, 'unsafe_xml')
        self.rejected(MODERN.replace('Witch','&unknown;'), 'format')

    def test_rejects_corrupt_base64_zlib_and_truncated_stream(self):
        for source in ['!!!', 'a', base64.b64encode(b'not zlib').decode(), encode(MODERN)[:-8]]:
            with self.subTest(source=source[:12]): self.rejected(source, 'format')
        trailing = base64.b64encode(zlib.compress(MODERN.encode())+b'trailing').decode()
        self.rejected(trailing, 'format')

    def test_input_and_decompression_limits(self):
        with patch.object(B,'MAX_INPUT',100):self.rejected(MODERN,'too_large')
        with patch.object(B,'MAX_XML',100):self.rejected(encode('x'*100000),'too_large')

    def test_xml_depth_and_complexity_limits(self):
        self.rejected('<a>'*65+'</a>'*65, 'too_large')
        self.rejected('<a>'+'<b/>'*50000+'</a>', 'too_large')

    def test_utf8_and_html_are_not_assumed_to_be_valid_builds(self):
        self.assertEqual(B.import_build('\ufeff'+MODERN)['class'],'Witch')
        self.rejected(MODERN.replace('Witch','Wi\x00tch'),'unsafe_xml')
        self.rejected('<html><body>Denied</body></html>','format')

    def test_url_allowlist_without_any_network_access(self):
        with patch.object(B.urllib.request,'build_opener') as net:
            for url in ['http://pobb.in/abcd','https://pobb.in.evil/abcd','https://pobb.in@127.0.0.1/abcd',
                        'https://pobb.in:443/abcd','https://pobb.in/../abcd','https://pobb.in/abcd?x=1',
                        'https://pobb.in/abcd#x','https://pobb.in/abcd/raw','https://127.0.0.1/abcd']:
                with self.subTest(url=url): self.rejected(url,'url')
            net.assert_not_called()

    def test_controlled_link_download_with_real_build(self):
        xml=(FIXTURES/'OccVortex.xml').read_text(encoding='utf-8')
        body=io.BytesIO(encode(xml).encode());body.headers={}
        with patch.object(B.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=body
            result=B.import_build('https://pobb.in/valid-code')
            req=opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url,'https://pobb.in/valid-code/raw')
        self.assertEqual(result['source'],'https://pobb.in/valid-code')
        self.assertEqual(result['sha256'],hashlib.sha256(xml.encode()).hexdigest())

    def test_oversized_http_body_even_without_content_length(self):
        body=io.BytesIO(b'x'*101);body.headers={}
        with patch.object(B,'MAX_INPUT',100),patch.object(B.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=body
            self.rejected('https://pobb.in/abcd','too_large')

    def test_network_errors_and_redirects_do_not_fetch_arbitrary_hosts(self):
        with patch.object(B.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.URLError('offline')
            self.rejected('https://pobb.in/abcd','network')
        with self.assertRaisesRegex(B.BuildImportError,'redirect'):
            B._NoRedirect().redirect_request(None,None,None,None,None,'http://127.0.0.1/')

    def test_untrusted_tree_url_is_not_exposed(self):
        result=B.import_build(MODERN.replace('https://www.pathofexile.com/passive-skill-tree/AAAA','javascript:alert(1)'))
        self.assertIsNone(result['trees'][1]['url'])


class BuildHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from poecraft.server import Handler
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.worker=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.worker.start()
        cls.url='http://127.0.0.1:'+str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.worker.join()

    def post(self,body,origin=None,path='/api/build/import'):
        req=urllib.request.Request(self.url+path,json.dumps(body).encode(),
                                   {'Content-Type':'application/json','Origin':origin or self.url,'X-Workshop-Build':'1'})
        try:
            with urllib.request.urlopen(req) as response:return response.status,json.load(response)
        except urllib.error.HTTPError as error:return error.code,json.load(error)

    def test_import_response_and_local_page(self):
        status,body=self.post({'source':MODERN});self.assertEqual(status,200);self.assertEqual(body['class'],'Witch')
        for path in ['/build','/build.js','/build.css']:
            with urllib.request.urlopen(self.url+path) as response:self.assertEqual(response.status,200)

    def test_cross_origin_and_arbitrary_parameters_rejected(self):
        self.assertEqual(self.post({'source':MODERN},'https://evil.example')[0],403)
        self.assertEqual(self.post({'path':'C:/private.xml'})[0],400)
        self.assertEqual(self.post([])[0],400)

    def test_error_code_is_localizable_and_hides_network_details(self):
        with patch.object(B,'_download',side_effect=B.BuildImportError('network')):
            status,body=self.post({'source':'https://pobb.in/abcd'})
        self.assertEqual((status,body),(400,{'error':'build.error.network'}))

    def test_all_build_strings_present_in_all_locales(self):
        from poecraft import config
        strings={lang:json.loads((config.PACKAGE_DIR/'locales'/f'{lang}.json').read_text(encoding='utf-8')) for lang in ['en','ru','de']}
        keys={k for k in strings['en'] if k.startswith(('build.','hub.tool.build.'))}
        for lang,data in strings.items():self.assertTrue(keys<=data.keys(),lang)

    def test_tree_api_checks_origin_before_loading(self):
        from poecraft import buildtree
        with patch.object(buildtree,'load_tree',return_value={'version':'3_29','nodes':{}}) as load:
            self.assertEqual(self.post({'version':'3_29'},'https://evil.example','/api/build/tree')[0],403)
            load.assert_not_called()
            status,body=self.post({'version':'3_29'},path='/api/build/tree')
            self.assertEqual((status,body['version']),(200,'3_29'))
            load.assert_called_once_with('3_29')

    def test_tree_network_error_is_localizable(self):
        from poecraft import buildtree
        with patch.object(buildtree,'load_tree',side_effect=buildtree.TreeDataError('network')):
            self.assertEqual(self.post({'version':'3_29'},path='/api/build/tree'),
                             (503,{'error':'build.map.error.network'}))

    def test_trade_api_origin_league_and_unsupported_item(self):
        from poecraft import buildtrade
        body={'item':{'text':'Rarity: NORMAL\nCoral Ring'},'league':'Covens'}
        with patch.object(buildtrade,'search',return_value={'url':'https://www.pathofexile.com/trade/search/Covens/q1','notes':[]}) as search:
            self.assertEqual(self.post(body,'https://evil.example','/api/build/trade')[0],403)
            search.assert_not_called()
            self.assertEqual(self.post(body,path='/api/build/trade')[0],200)
            search.assert_called_once_with(body['item'],'Covens')
        with patch.object(buildtrade.evaluator,'evaluate') as evaluate:
            self.assertEqual(self.post({'item':{'text':'{variant:1}unknown'},'league':'Covens'},path='/api/build/trade')[0],400)
            evaluate.assert_not_called()
