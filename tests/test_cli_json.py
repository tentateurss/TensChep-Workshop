import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


# Exercise CLI dispatch and real file redirection without depending on game weights.
SCRIPT = '''
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch
from poecraft import __main__ as cli

mode = sys.argv[1]
sys.stdout.reconfigure(encoding='utf-8' if mode == 'human' else 'cp1251', errors='strict')
result = {'have': ['ColdResist6'], 'пояснение': 'Сохранить → продолжить',
          'nested': {'cost': 12.5, 'supported': True, 'missing': None}}
data = SimpleNamespace(base_by_name=lambda name: SimpleNamespace(id='base'))
planner = Mock()
planner.recover.return_value = planner.plan.return_value = result
def targets(*args):
    print('diagnostic: Пример → выбор')
    return [{'signature': 'suffix|cold_resistance', 'min': 40}]
args = ['plan', '--base', 'Vaal Regalia', '--target', 'Cold Resistance>=40',
        '--sims', '0', '--no-live-prices', '--quiet']
if mode != 'plan': args += ['--have', 'ColdResist6']
if mode != 'human': args += ['--json']
with patch('poecraft.data.repository.get_data', return_value=data), \
     patch('poecraft.data.repository.get_rules', return_value={}), \
     patch('poecraft.engine.context.ItemContext'), \
     patch('poecraft.engine.planner.Planner', return_value=planner), \
     patch('poecraft.engine.prices.Prices'), \
     patch.object(cli, '_targets_from_cli', side_effect=targets), \
     patch.object(cli, '_present', side_effect=lambda raw: raw), \
     patch.object(cli, '_print_recovery', side_effect=lambda raw: print(raw['пояснение'])):
    raise SystemExit(cli.main(args))
'''


class CliJson(unittest.TestCase):
    def run_cli(self, mode):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'output.json'
            with path.open('wb') as out:
                child = subprocess.run([sys.executable, '-c', SCRIPT, mode], stdout=out,
                                       stderr=subprocess.PIPE, timeout=30,
                                       env=dict(os.environ, PYTHONIOENCODING='cp1251:strict'))
            self.assertEqual(child.returncode, 0, child.stderr.decode('cp1251', errors='replace'))
            if mode == 'human':
                return path.read_text(encoding='utf-8'), child.stderr
            with path.open(encoding='cp1251') as source:
                decoded = json.load(source)
            self.assertTrue(path.read_bytes().isascii())
            self.assertIn(b'\\u2192', path.read_bytes())
            self.assertEqual(decoded, {'have': ['ColdResist6'], 'пояснение': 'Сохранить → продолжить',
                                       'nested': {'cost': 12.5, 'supported': True, 'missing': None}})
            self.assertIn(b'diagnostic:', child.stderr)

    def test_recovery_json_cp1251_file(self):
        self.run_cli('recovery')

    def test_plan_json_cp1251_file(self):
        self.run_cli('plan')

    def test_human_output_unchanged(self):
        output, errors = self.run_cli('human')
        self.assertEqual(output, 'diagnostic: Пример → выбор\nСохранить → продолжить\n')
        self.assertEqual(errors, b'')
