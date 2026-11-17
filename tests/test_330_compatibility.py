"""Source-format regressions; fixtures are subsets of the 3.29/3.30 exports."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from poecraft.data import special, timeless
from poecraft.engine import evaluator, timeless as calculator
from poecraft.messages import render

FIXTURES = Path(__file__).parent / 'fixtures'


class SourceFormats(unittest.TestCase):
    def test_legacy_russian_seed_is_explicitly_supported(self):
        templates = json.loads((FIXTURES / 'timeless_ru_329.json').read_text(encoding='utf-8'))
        with patch.object(timeless, 'available', return_value=True), patch.object(timeless, 'conquerors', return_value=templates):
            info = calculator.parse('\u041e\u043c\u044b\u0442 \u0432 \u043a\u0440\u043e\u0432\u0438 2000 \u0436\u0435\u0440\u0442\u0432 \u0432\u043e \u0438\u043c\u044f \u0428\u0438\u0431\u0430\u043a\u0432\u044b')
        self.assertEqual((info['conqueror'], info['seed']), ('Xibaqua', 2000))

    def test_unresolved_330_handler_does_not_reuse_legacy_names(self):
        source = json.loads((FIXTURES / 'timeless_ru_330.json').read_text(encoding='utf-8'))
        self.assertIn('{2}', source[0]['Russian'][0]['string'])
        templates = timeless.seed_templates(source, 'ru')
        self.assertEqual(templates, [])  # no keystone condition: handler mapping is not exported
        legacy = json.loads((FIXTURES / 'timeless_ru_329.json').read_text(encoding='utf-8'))['ru'][0]['line']
        with patch.object(timeless, 'available', return_value=True), patch.object(timeless, 'conquerors', return_value={'ru': templates}):
            self.assertIsNone(calculator.parse(legacy.replace('{1}', '2000')))
        result = evaluator._timeless({'warnings': []}, None, {'league': 'Covens'})
        self.assertEqual(result['calculation_status'], 'not computed')
        self.assertNotIn('timeless', result)
        for lang in ('en', 'ru', 'de'):
            shown = render(result, lang)
            self.assertTrue(shown['warnings'])
            self.assertIn(shown['warnings'][0], shown['message'])  # visible notice, not only collapsed notes
            self.assertNotIn('m.eval.timeless_unresolved', str(shown))

    def test_aspect_zero_stats_are_preserved_without_invented_effect_text(self):
        for name in ('of Farrul', 'of Saqawal', 'of Fenumus', 'of Craiceann'):
            with self.subTest(name=name):
                raw = {'name': name, 'stats': [{'id': 'display_stat', 'min': 0, 'max': 0}],
                       'grants_effects': [{'granted_effect_id': 'source_skill', 'level': 20}]}
                converted = special._raw_mod('source_id', raw)
                self.assertEqual(converted['text'], name)
                self.assertEqual(converted['stats'], raw['stats'])
                self.assertNotIn('20', converted['text'])
                raw['text'] = 'Source-provided description'
                self.assertEqual(special._raw_mod('source_id', raw)['text'], raw['text'])

    def test_old_special_cache_is_rebuilt_instead_of_retaining_blank_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'mods.json').write_text(json.dumps({'cat': {
                'name': 'of Farrul', 'domain': 'item', 'stats': [{'id': 'cat_stat', 'min': 0, 'max': 0}]}}))
            (root / 'fossils.json').write_text('{}')
            stamp = [{'size': p.stat().st_size, 'mtime': p.stat().st_mtime}
                     for p in (root / 'mods.json', root / 'fossils.json')]
            cache = root / 'cache.json'
            cache.write_text(json.dumps({'source': stamp, 'data': {'beast_outcomes': [], 'fossils': [], 'recipes': []}}))
            special.get_special.cache_clear()
            try:
                with patch.object(special, 'CACHE_PATH', cache):
                    result = special.get_special(root)
                self.assertEqual(result.beast_outcomes[0].text, 'of Farrul')
                self.assertEqual(json.loads(cache.read_text())['schema'], 2)
            finally:
                special.get_special.cache_clear()
