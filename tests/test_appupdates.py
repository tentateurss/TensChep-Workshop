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
