import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location('prepare_release_data',
    Path(__file__).parents[1] / 'packaging/prepare_release_data.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseData(unittest.TestCase):
    def snapshot(self):
        bodies = {name: b'{"fixture":1}' for name in ['data/' + n + '.json' for n in release.config.REPOE_FILES]}
        bodies.update({'version.txt': b'3.30.0.4', 'exported-version.txt': b'3.30.0.4\n'})
        lock = {'version': '3.30.0.4', 'repository': 'repoe-fork/repoe-fork.github.io',
                'revision': 'c' * 40, 'files': {name: {'bytes': len(body),
                'sha256': hashlib.sha256(body).hexdigest()} for name, body in bodies.items()}}
        return lock, bodies

    def test_immutable_export_does_not_follow_latest_version(self):
        lock, bodies = self.snapshot()
        urls = []
        def open_url(request, **kwargs):
            urls.append(request.full_url)
            self.assertIn('/' + lock['revision'] + '/', request.full_url)
            name = request.full_url.split('/' + lock['revision'] + '/')[1]
            return io.BytesIO(bodies[name])
        with tempfile.TemporaryDirectory() as td, patch.object(release.urllib.request, 'urlopen', side_effect=open_url):
            release.download(lock, Path(td))
            self.assertEqual(json.loads((Path(td) / 'data/mods.json').read_bytes()), {'fixture': 1})
        self.assertEqual(len(urls), len(bodies))

    def test_corrupt_download_is_rejected_before_staging(self):
        lock, _ = self.snapshot()
        with tempfile.TemporaryDirectory() as td, patch.object(release.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
            with self.assertRaisesRegex(ValueError, 'checksum'):
                release.download(lock, Path(td))
            self.assertFalse((Path(td) / 'version.txt').exists())

    def test_valid_hash_cannot_hide_wrong_version(self):
        lock, bodies = self.snapshot()
        body = b'3.30.0.5'
        bodies['version.txt'] = body
        lock['files']['version.txt'] = {'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        def open_url(request, **kwargs):
            return io.BytesIO(bodies[request.full_url.split('/' + lock['revision'] + '/')[1]])
        with tempfile.TemporaryDirectory() as td, patch.object(release.urllib.request, 'urlopen', side_effect=open_url):
            with self.assertRaisesRegex(ValueError, 'version mismatch'):
                release.download(lock, Path(td))

    def test_published_min_format_omits_only_null_object_fields(self):
        original = {'influence_tags': None, 'stats': [{'min': 0, 'max': 1, 'unused': None}],
                    'enabled': False, 'name': '', 'weights': [], 'cost': {'divine': 4}}
        self.assertEqual(release.minified_data(original), {'stats': [{'min': 0, 'max': 1}],
                    'enabled': False, 'name': '', 'weights': [], 'cost': {'divine': 4}})
        self.assertIn('influence_tags', original)

    def test_mutable_revision_and_missing_sources_are_rejected(self):
        lock, _ = self.snapshot()
        with tempfile.TemporaryDirectory() as td, patch.object(release.urllib.request, 'urlopen') as network:
            lock['revision'] = 'master'
            with self.assertRaises(ValueError): release.download(lock, Path(td))
            lock['revision'] = 'c' * 40
            del lock['files']['data/mods.json']
            with self.assertRaises(ValueError): release.download(lock, Path(td))
            network.assert_not_called()
