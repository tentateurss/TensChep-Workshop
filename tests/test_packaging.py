import importlib.util
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

class PackagedPaths(unittest.TestCase):
    def test_release_archive_rejects_private_and_development_files(self):
        import zipfile
        spec=importlib.util.spec_from_file_location('check_archive',Path(__file__).parents[1]/'packaging/check_archive.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            archive=Path(td)/'portable.zip'
            for name in ['tests/test_engine.py','data/poecraft.db','.env','id_ed25519','_internal/cache.log']:
                with self.subTest(name=name):
                    with zipfile.ZipFile(archive,'w') as z:z.writestr('TensChep-Workshop/'+name,'sentinel')
                    with self.assertRaises(ValueError):module.check(archive)

    def test_release_archive_keeps_runtime_resources(self):
        import zipfile
        spec=importlib.util.spec_from_file_location('check_archive',Path(__file__).parents[1]/'packaging/check_archive.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as td:
            archive=Path(td)/'portable.zip'
            with zipfile.ZipFile(archive,'w') as z:
                for name in ['TensChep-Workshop.exe','PYTHON-LICENSE.txt','_internal/poecraft/web/app.js','_internal/poecraft/locales/ru.json']:
                    z.writestr('TensChep-Workshop/'+name,'resource')
            module.check(archive)

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
