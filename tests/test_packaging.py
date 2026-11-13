import importlib.util
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

class PackagedPaths(unittest.TestCase):
    def test_packaged_data_uses_user_profile_not_bundle(self):
        import launcher
        with tempfile.TemporaryDirectory() as td, patch.dict(os.environ, {'LOCALAPPDATA':td}, clear=True), patch.object(launcher.sys,'frozen',True,create=True):
            launcher.prepare()
            root=Path(td)/'TensChep Workshop'
            self.assertEqual(Path(os.environ['POECRAFT_DATA_DIR']),root/'data')
            prices=root/'config/prices.json'
            self.assertTrue(prices.exists())
            prices.write_text('user override')
            launcher.prepare()
            self.assertEqual(prices.read_text(),'user override')
