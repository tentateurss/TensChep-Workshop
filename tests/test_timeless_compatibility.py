import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from poecraft.data.timeless_status import status,REQUIRED,require
from poecraft.data import timeless

class TimelessCompatibility(unittest.TestCase):
 def fixture(self,root,game='3.29.3.6'):
  for name in REQUIRED:(root/name).write_bytes(b'table')
  (root/'tree.json').write_text(json.dumps({'nodes':{'1':{'kind':'notable'}}}))
  (root/'index.json').write_text(json.dumps({'nodes':{'1':[0,1]}}))
  (root/'legion.json').write_text('{"nodes":[]}')
  meta={'game_version':game,'tree_game_version':game,'revisions':{'pob':'a'*40,'tree':'c'*40},'sources':{'pob':'https://example.invalid/pinned'},'sha256':{n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in REQUIRED}}
  (root/'manifest.json').write_text(json.dumps(meta));return meta
 def test_matching_versions(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);self.fixture(r);self.assertEqual(status(r,'3.29.3.6')['compatibility'],'compatible')
 def test_version_mismatch(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);self.fixture(r);self.assertEqual(status(r,'3.30.0')['compatibility'],'incompatible')
 def test_unknown_version(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);self.fixture(r,None);self.assertEqual(status(r,'3.30.0')['compatibility'],'unverified')
 def test_missing_and_checksum_changes(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);self.fixture(r);(r/'lut_1.bin').write_bytes(b'changed')
   self.assertEqual(status(r,'3.29.3.6')['reason'],'checksum_mismatch')
   (r/'lut_2.bin').unlink();self.assertEqual(status(r,'3.29.3.6')['compatibility'],'missing')
 def test_revision_invalidates_caches(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);meta=self.fixture(r)
   first=status(r,'3.29.3.6')['fingerprint'];meta['revisions']['pob']='b'*40;(r/'manifest.json').write_text(json.dumps(meta))
   with patch.object(timeless,'_clear') as clear:
    second=status(r,'3.29.3.6')['fingerprint'];clear.assert_called_once()
   self.assertNotEqual(first,second)
 def test_tree_mapping_mismatch(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);self.fixture(r);(r/'index.json').write_text('{"nodes":{}}')
   self.assertEqual(status(r,'3.29.3.6')['reason'],'tree_mismatch')
 def test_download_failure_preserves_previous(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d)/'timeless';root.mkdir();(root/'sentinel').write_text('old')
   with patch.object(timeless,'_revision',return_value=None),patch.object(timeless,'_get',side_effect=OSError('offline')):
    with self.assertRaises(OSError):timeless.update(root,log=lambda *args:None)
   self.assertEqual((root/'sentinel').read_text(),'old')
 def test_incompatible_data_blocks_calculation(self):
  with patch('poecraft.data.timeless_status.status',return_value={'compatibility':'incompatible','reason':'version_mismatch'}):
   with self.assertRaises(timeless.TimelessDataMissing):require()
 def test_known_legacy_heroic_is_incompatible_with_330(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);self.fixture(r,None)
   (r/'legion.json').write_text(json.dumps({'nodes':[{'name':'Starlight Swig','sd':['15% chance for Ward to be Restored when you Use a Flask']}]}))
   self.assertEqual(status(r,'3.30.0')['reason'],'legacy_heroic')
 def test_api_warning_removes_old_calculation(self):
  from poecraft.engine.evaluator import _timeless_extras
  from poecraft.messages import render
  st={'compatibility':'incompatible','reason':'version_mismatch','game_version':'3.30.0'}
  with patch('poecraft.data.timeless_status.status',return_value=st):
   out=_timeless_extras({'timeless':{'calc':{'old':True},'equivalents':{},'data':True},'warnings':[]},None,None,{})
  self.assertNotIn('calc',out['timeless']);self.assertFalse(out['timeless']['data'])
  for lang in ('en','ru','de'):
   result=render(out,lang);self.assertTrue(result['warnings']);self.assertNotIn('m.tj.',result['warnings'][0])
 def test_evaluator_cache_uses_table_fingerprint(self):
  from poecraft.engine import evaluator
  with patch.dict(evaluator._CACHE,{},clear=True),patch.object(evaluator.trade,'current_league',return_value={'league':'Test','source':'trade','known':['Test']}),patch.object(evaluator,'_price_league',return_value=None),patch.object(evaluator,'_evaluate',return_value={'kind':'other','warnings':[],'listings':[]}) as compute:
   for fingerprint in ('first','second'):
    with patch('poecraft.data.timeless_status.status',return_value={'fingerprint':fingerprint}):
     evaluator.evaluate('test item')
   self.assertEqual(compute.call_count,2)
 def test_downloads_are_pinned_to_resolved_revisions(self):
  with patch.object(timeless,'_revision',return_value='a'*40),patch.object(timeless,'_update',return_value={}) as update:
   old=timeless.POB_DIR
   def inspect(*args):
    self.assertIn('a'*40,timeless.POB_DIR);self.assertIn('a'*40,timeless.TREE_URL);return {}
   update.side_effect=inspect;timeless.update(Path('unused'))
   self.assertEqual(timeless.POB_DIR,old)
 def test_missing_equivalent_column_is_not_silently_ignored(self):
  from poecraft.engine import timeless as engine
  with patch('poecraft.data.timeless_status.require',return_value={'compatibility':'unverified'}),patch.object(engine,'in_radius',return_value=[('1',{'kind':'notable'})]),patch.object(timeless,'column',return_value=None):
   with self.assertRaises(timeless.TimelessDataMissing):engine.equivalents(2,10000,'socket')
 def test_malformed_manifest_groups_are_incompatible(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);original=self.fixture(root)
   for key in ('revisions','sources','sha256'):
    for value in (None,[], 'invalid'):
     with self.subTest(key=key,value=value):
      meta=dict(original,**{key:value});(root/'manifest.json').write_text(json.dumps(meta))
      result=status(root,'3.29.3.6')
      self.assertEqual(result['compatibility'],'incompatible')
      self.assertEqual(result['reason'],'invalid_manifest')
 def test_unknown_revision_in_valid_manifest_is_not_corruption(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);meta=self.fixture(root);meta['revisions']={'pob':None,'tree':None}
   (root/'manifest.json').write_text(json.dumps(meta))
   self.assertEqual(status(root,'3.29.3.6')['compatibility'],'unverified')
 def test_invalid_timeless_manifest_does_not_break_other_evaluations(self):
  from poecraft.engine import evaluator
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);meta=self.fixture(root);meta['revisions']=None;(root/'manifest.json').write_text(json.dumps(meta))
   with patch.object(timeless,'TIMELESS_DIR',root),patch.dict(evaluator._CACHE,{},clear=True),patch.object(evaluator.trade,'current_league',return_value={'league':'Test','source':'trade','known':['Test']}),patch.object(evaluator,'_price_league',return_value=None),patch.object(evaluator,'_evaluate',return_value={'kind':'other','warnings':[],'listings':[]}) as compute:
    self.assertEqual(evaluator.evaluate('Chaos Orb')['kind'],'other')
    compute.assert_called_once()
