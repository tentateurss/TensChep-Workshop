import unittest
from poecraft.appupdates import version,select

def release(tag,**extra):
 return dict(tag_name=tag,assets=[{'name':'TensChep-Workshop-Windows-x64.zip'},{'name':'SHA256SUMS.txt'}],**extra)

class ReleaseChecking(unittest.TestCase):
 def test_semantic_order(self):
  tags=['v1.0.0-beta.1','v1.0.0-beta.2','v1.0.0-beta.10','v1.0.0','v1.0.1']
  self.assertEqual(sorted(tags,key=version),tags)
 def test_beta_selects_highest(self):
  self.assertEqual(select([release('v1.0.0-beta.2',prerelease=True),release('v1.0.0-beta.10',prerelease=True)],'1.0.0-beta.1','beta')['version'],'v1.0.0-beta.10')
 def test_stable_rejects_beta_and_draft(self):
  self.assertIsNone(select([release('v1.0.1',draft=True),release('v1.1.0-beta.1',prerelease=True)],'1.0.0','stable'))
 def test_downgrade_invalid_and_missing_assets(self):
  self.assertIsNone(select([release('v0.9.0'),release('garbage'),dict(tag_name='v2.0.0',assets=[])],'1.0.0','beta'))
 def test_invalid_channel(self):
  with self.assertRaises(ValueError):select([],'1.0.0','other')

class ReleaseNetwork(unittest.TestCase):
 def test_offline_does_not_block_and_saves_channel(self):
  import tempfile,time,json
  from pathlib import Path
  from unittest.mock import patch
  from poecraft import appupdates
  with tempfile.TemporaryDirectory() as d, patch.object(appupdates.config,'CONFIG_DIR',Path(d)), patch.object(appupdates.urllib.request,'urlopen',side_effect=OSError('offline')):
   with appupdates._lock:appupdates._status['state']='idle'
   appupdates.check(force=True,channel='stable')
   for _ in range(100):
    if appupdates.status()['state']!='checking' and (Path(d)/'application-updates.json').exists():break
    time.sleep(.01)
   self.assertEqual(appupdates.status()['state'],'error')
   self.assertEqual(json.loads((Path(d)/'application-updates.json').read_text())['channel'],'stable')
 def test_recent_cache_avoids_network(self):
  import tempfile,time,json
  from pathlib import Path
  from unittest.mock import patch
  from poecraft import appupdates
  with tempfile.TemporaryDirectory() as d, patch.object(appupdates.config,'CONFIG_DIR',Path(d)), patch.object(appupdates.urllib.request,'urlopen') as network:
   (Path(d)/'application-updates.json').write_text(json.dumps({'channel':'beta','checked_at':time.time(),'state':'current'}))
   with appupdates._lock:appupdates._status['state']='idle'
   self.assertEqual(appupdates.check()['state'],'current')
   network.assert_not_called()
