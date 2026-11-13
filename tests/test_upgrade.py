"""Regression coverage for safe league transitions."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from poecraft.data import ninja
from poecraft.data.update import update
from poecraft.engine.planner import Planner
from types import SimpleNamespace

class UpgradeSafety(unittest.TestCase):
    def test_version_mismatch_never_stages(self):
        with patch('poecraft.data.update.source_version', return_value='3.29.3.6'), patch('poecraft.data.update.tempfile.mkdtemp') as stage:
            with self.assertRaisesRegex(ValueError, 'WAITING FOR GAME DATA'):
                update('https://example.invalid', '3.30')
            stage.assert_not_called()

    def test_dry_run_never_stages(self):
        with patch('poecraft.data.update.source_version', return_value='3.30.0'), patch('poecraft.data.update.tempfile.mkdtemp') as stage:
            update('https://example.invalid', '3.30', True)
            stage.assert_not_called()

    def test_old_league_snapshot_is_not_a_price(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text(json.dumps({'_meta': {'league': 'Allflame', 'fetched_at': ninja._now_iso()}, 'prices': {'Divine Orb': 200}}))
            with patch.dict(os.environ, {'POECRAFT_LEAGUE': 'Covens of Umbra'}):
                status = ninja.load_status(path)
            self.assertEqual(status['state'], 'unavailable')
            self.assertEqual(status['prices'], {})

    def test_bench_recipe_respects_class_and_all_currencies(self):
        planner = object.__new__(Planner)
        planner.data = SimpleNamespace(bench_crafts=[
            SimpleNamespace(mod_id='lock', item_classes=['Ring'], cost=[('Divine Orb', 4), ('Exalted Orb', 8)]),
            SimpleNamespace(mod_id='lock', item_classes=['Bow'], cost=[('Divine Orb', 4), ('Sacred Orb', 1)])])
        planner.prices = SimpleNamespace(basket=lambda xs: sum({'Divine Orb': 100, 'Exalted Orb': 10, 'Sacred Orb': 3}[name]*count for name,count in xs))
        self.assertEqual(planner._craft_cost('lock', 'Ring'), 480)
        self.assertEqual(planner._craft_cost('lock', 'Bow'), 403)
        self.assertIsNone(planner._craft_cost('lock', 'Boots'))

    def test_failed_download_preserves_active_database(self):
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / 'data'
            active.mkdir()
            database = active / 'poecraft.db'
            database.write_bytes(b'user database sentinel')
            with patch('poecraft.data.update.config.DATA_DIR', active), patch('poecraft.data.update.source_version', return_value='3.30.0'), patch('poecraft.data.update.subprocess.run', side_effect=RuntimeError('download failed')):
                with self.assertRaisesRegex(RuntimeError, 'download failed'):
                    update('https://example.invalid', '3.30')
            self.assertEqual(database.read_bytes(), b'user database sentinel')

class AutomaticLeaguePrices(unittest.TestCase):
    def test_automatic_transition_failed_categories_rejects_old_prices(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text(json.dumps({'_meta': {'league': 'Old', 'fetched_at': ninja._now_iso()}, 'prices': {'Divine Orb': 999}}))
            with patch.dict(os.environ, {'POECRAFT_LEAGUE': ''}), patch.object(ninja, 'resolve_league', return_value='New'), patch.object(ninja, '_get_json', side_effect=ninja.NinjaError('offline')):
                status = ninja.update(path=path)
            self.assertEqual(status['prices'], {})
            self.assertEqual(status['league'], 'New')
            self.assertEqual(status['state'], 'unavailable')
            self.assertEqual(json.loads(path.read_text())['prices']['Divine Orb'], 999)

    def test_unknown_automatic_league_offline_rejects_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text(json.dumps({'_meta': {'league': 'Old', 'fetched_at': ninja._now_iso()}, 'prices': {'Divine Orb': 999}}))
            with patch.dict(os.environ, {'POECRAFT_LEAGUE': ''}), patch.object(ninja, 'resolve_league', side_effect=ninja.NinjaError('offline')):
                self.assertEqual(ninja.update(path=path)['prices'], {})

    def test_same_league_offline_categories_can_use_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text(json.dumps({'_meta': {'source': ninja.API, 'league': 'Same', 'fetched_at': ninja._now_iso()}, 'prices': {'Divine Orb': 123}}))
            with patch.dict(os.environ, {'POECRAFT_LEAGUE': ''}), patch.object(ninja, 'resolve_league', return_value='Same'), patch.object(ninja, '_get_json', side_effect=ninja.NinjaError('offline')):
                status = ninja.update(path=path)
            self.assertEqual(status['state'], 'stale')
            self.assertEqual(status['prices']['Divine Orb'], 123)

    def test_cache_with_other_url_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'same.json'
            path.write_text(json.dumps({'url': 'https://other.invalid', 'checked_at': ninja.time.time(), 'ttl': 300, 'body': {'wrong': True}}))
            with patch.object(ninja.config, 'NINJA_CACHE_DIR', Path(directory)), patch.object(ninja.urllib.request, 'urlopen', side_effect=OSError('offline')), patch.object(ninja.time, 'sleep'):
                with self.assertRaises(ninja.NinjaError):
                    ninja._get_json('https://expected.invalid', 'same', 300)

class PriceContext(unittest.TestCase):
    def test_first_launch_unverified_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            path.write_text(json.dumps({'_meta': {'league': 'L', 'fetched_at': ninja._now_iso()}, 'prices': {'Divine Orb': 999}}))
            self.assertEqual(ninja.load_status(path)['prices'], {})

    def test_confirmed_snapshot_scoped_to_selected_league_and_source(self):
        from poecraft.engine.prices import Prices, PRICE_LEAGUE
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prices.json'
            snap = {'_meta': {'source': ninja.API, 'league': 'L', 'fetched_at': ninja._now_iso(), 'last_attempt': {'selection_verified': True, 'requested_league': 'L', 'ok': True}}, 'prices': {'Divine Orb': 999}}
            path.write_text(json.dumps(snap))
            with patch.dict(os.environ, {'POECRAFT_LEAGUE': ''}):
                self.assertEqual(Prices(live_path=path, league='L').get('Divine Orb'), 999)
                token = PRICE_LEAGUE.set('Other')
                try:
                    self.assertEqual(Prices(live_path=path).live, {})
                finally:
                    PRICE_LEAGUE.reset(token)
                snap['_meta']['source'] = 'https://other.invalid'
                path.write_text(json.dumps(snap))
                self.assertEqual(Prices(live_path=path, league='L').live, {})

class EvaluatorLeagueContext(unittest.TestCase):
    def test_selected_league_reaches_all_price_reads_and_context_is_reset(self):
        from poecraft.engine import evaluator
        from poecraft.engine.prices import PRICE_LEAGUE
        seen = []
        def compute(*args):
            seen.append(PRICE_LEAGUE.get())
            return {'warnings': [], 'league': args[1]}
        with patch.object(evaluator.trade, 'current_league', return_value={'league': 'Default', 'known': ['Selected'], 'source': 'trade'}), patch.object(evaluator, '_price_league', return_value=None), patch.object(evaluator, '_evaluate_with_context', side_effect=compute):
            result = evaluator.evaluate('item', league='Selected')
        self.assertEqual(seen, ['Selected'])
        self.assertIsNone(PRICE_LEAGUE.get())
        self.assertIn('economy', result)

class StagingCompatibility(unittest.TestCase):
    def test_repeated_quarantine_preserves_settings_and_removes_old_locale(self):
        from poecraft.data.compatibility import quarantine_inherited
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'templates.json').write_text('user')
            for text in ['old', 'second']:
                (root / 'locale_game_ru.json').write_text(text)
                quarantine_inherited(root)
                self.assertFalse((root / 'locale_game_ru.json').exists())
            self.assertEqual((root / 'templates.json').read_text(), 'user')
            self.assertEqual(len(list((root / 'inherited-data').glob('*/locale_game_ru.json'))), 2)

    def test_mandatory_checksum_failure_blocks_inventory(self):
        from poecraft.data.compatibility import inventory
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'raw').mkdir()
            (root/'raw'/'mods.json').write_text('{}')
            (root/'raw'/'manifest.json').write_text(json.dumps({'files': {'mods': {'sha256': 'wrong'}}}))
            with self.assertRaisesRegex(ValueError, 'checksum'):
                inventory(root, '3.30', 'https://example.invalid')
            self.assertFalse((root/'compatibility.json').exists())

    def test_missing_and_incompatible_optional_locales_are_not_active(self):
        from poecraft.data.compatibility import inventory
        from poecraft.data.fetch import _sha256
        from poecraft import config
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); raw = root/'raw';raw.mkdir()
            files = {}
            for name in config.REPOE_FILES:
                path=raw/(name+'.json');path.write_text('{"synthetic": {}}')
                files[name]={'sha256':_sha256(path)}
            (raw/'manifest.json').write_text(json.dumps({'files':files}))
            (root/'locale_game_ru.json').write_text('old')
            with patch('poecraft.data.compatibility.locales.build', side_effect=[ValueError('different build'), FileNotFoundError('offline')]):
                report=inventory(root,'3.30','https://example.invalid')
            self.assertEqual(report['sources']['locale_ru']['compatibility'],'incompatible')
            self.assertEqual(report['sources']['locale_de']['compatibility'],'missing')
            self.assertFalse((root/'locale_game_ru.json').exists())
            self.assertTrue((root/'rejected-locales'/'locale_game_ru.json').exists())
            self.assertFalse(report['fully_compatible'])

class PromotionSafety(unittest.TestCase):
    def test_promotion_failure_restores_database_and_templates(self):
        import sqlite3
        from poecraft.data import update as updater
        with tempfile.TemporaryDirectory() as d:
            active=Path(d)/'data';active.mkdir()
            (active/'poecraft.db').write_bytes(b'original')
            (active/'templates.json').write_text('personal')
            def prepare(args, env, check):
                root=Path(env['POECRAFT_DATA_DIR']);(root/'poecraft.db').unlink()
                with sqlite3.connect(root/'poecraft.db') as db:
                    db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
                    for name in ('bases','mods','essences','fossils','bench_crafts'):
                        db.execute('CREATE TABLE '+name+'(id TEXT)');db.execute('INSERT INTO '+name+' VALUES ("test")')
                db.close()
                (root/'raw').mkdir();(root/'raw'/'manifest.json').write_text('{}')
            rename=Path.rename
            def fail_stage(path,target):
                if path.name.startswith('.poecraft-stage-'):
                    raise OSError('promotion failed')
                return rename(path,target)
            with patch.object(updater.config,'DATA_DIR',active), patch.object(updater,'source_version',return_value='3.30.0'), patch.object(updater.subprocess,'run',side_effect=prepare), patch('poecraft.data.compatibility.inventory',return_value={}), patch.object(Path,'rename',fail_stage):
                with self.assertRaisesRegex(OSError,'promotion failed'):
                    updater.update('https://example.invalid','3.30')
            self.assertEqual((active/'poecraft.db').read_bytes(),b'original')
            self.assertEqual((active/'templates.json').read_text(),'personal')
