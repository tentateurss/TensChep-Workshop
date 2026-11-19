import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
import urllib.error

from poecraft.data import ninja as N


class PriceFreshness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1800000000
        self.calls = 0
        self.headers = {'Age': '1756', 'Cache-Control': 'max-age=1800', 'ETag': 'test'}
        self.error = None
        self.body = {'core': {'primary': 'chaos'}, 'items': [{'id': 'div', 'name': 'Divine Orb'}],
                     'lines': [{'id': 'div', 'primaryValue': 200}]}
        for p in (patch.object(N.config, 'NINJA_CACHE_DIR', self.root / 'raw'),
                  patch.object(N, 'CATEGORIES', ['Currency']),
                  patch.object(N, 'resolve_league', side_effect=lambda league: league),
                  patch.object(N.time, 'time', side_effect=lambda: self.now),
                  patch.object(N.time, 'sleep'),
                  patch.object(N.urllib.request, 'urlopen', side_effect=self.response)):
            p.start()
            self.addCleanup(p.stop)

    def response(self, *args, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        result = io.BytesIO(json.dumps(self.body).encode())
        result.headers = self.headers.copy()
        return result

    def update(self, league='Covens'):
        return N.update(league, self.root / 'snapshot.json')

    def test_cached_reads_keep_receipt_and_http_age(self):
        first = self.update()
        self.assertEqual(first['state'], 'live')
        self.assertEqual(first['fetched_at'], N._now_iso(self.now))
        self.assertEqual(first['validated_at'], N._now_iso(self.now - 1756))
        self.assertEqual(first['expires_at'], self.now + 44)
        raw = next((self.root / 'raw').glob('*.json'))
        saved = raw.read_bytes()
        self.now += 10
        second = self.update()
        self.assertEqual(first['fetched_at'], second['fetched_at'])
        self.assertEqual(first['expires_at'], second['expires_at'])
        self.assertEqual(raw.read_bytes(), saved)
        self.assertEqual(self.calls, 1)

    def test_restart_reads_persisted_snapshot_without_rejuvenation(self):
        first = self.update()
        self.now += 45
        # A fresh reader uses only persisted metadata, with no process-local timestamps.
        status = N.load_status(self.root / 'snapshot.json', 'Covens')
        self.assertEqual(status['state'], 'stale')
        self.assertEqual(status['fetched_at'], first['fetched_at'])
        self.assertEqual(self.update()['state'], 'stale')
        self.assertEqual(self.calls, 1)  # request cooldown does not extend freshness
        code = ('import json; from pathlib import Path; from unittest.mock import patch; '
                'from poecraft.data import ninja as n; '
                f'p=Path({str(self.root / "snapshot.json")!r}); '
                f'patch.object(n.time,"time",return_value={self.now}).start(); '
                'print(json.dumps(n.load_status(p,"Covens")))')
        restarted = json.loads(subprocess.check_output([sys.executable, '-c', code], text=True))
        self.assertEqual(restarted['state'], 'stale')
        self.assertEqual(restarted['fetched_at'], first['fetched_at'])

    def test_short_ttl_not_extended_to_request_cooldown(self):
        self.headers = {'Age': '0', 'Cache-Control': 'max-age=10'}
        first = self.update()
        self.assertEqual(first['expires_at'], self.now + 10)
        self.now += 11
        self.assertEqual(self.update()['state'], 'stale')
        self.assertEqual(self.calls, 1)

    def test_age_beyond_ttl_is_stale_immediately(self):
        self.headers['Age'] = '1900'
        self.assertEqual(self.update()['state'], 'stale')

    def test_date_apparent_age(self):
        from email.utils import formatdate
        self.headers = {'Age': '1', 'Date': formatdate(self.now - 1790, usegmt=True),
                        'Cache-Control': 'max-age=1800'}
        self.assertEqual(self.update()['expires_at'], self.now + 10)

    def test_invalid_http_age_is_unknown(self):
        for age in ('bad', '-1', 'NaN'):
            with self.subTest(age=age):
                self.headers['Age'] = age
                status = self.update(age)
                self.assertEqual(status['state'], 'unavailable')
                self.assertEqual(status['prices'], {})

    def test_304_validates_without_changing_acquisition(self):
        first = self.update()
        self.now += 301
        self.error = urllib.error.HTTPError('https://example.invalid', 304, 'Not modified',
                                           {'Age': '10', 'Cache-Control': 'max-age=100'}, None)
        status = self.update()
        self.assertEqual(status['fetched_at'], first['fetched_at'])
        self.assertEqual(status['validated_at'], N._now_iso(self.now - 10))
        self.assertEqual(status['expires_at'], self.now + 90)
        self.now += 1
        self.assertEqual(self.update()['expires_at'], status['expires_at'])
        self.assertEqual(self.calls, 2)

    def test_league_isolation(self):
        self.update('Covens')
        status = N.load_status(self.root / 'snapshot.json', 'Standard')
        self.assertEqual(status['prices'], {})
        self.update('Standard')
        self.assertEqual(self.calls, 2)
        self.assertEqual(len(list((self.root / 'raw').glob('*.json'))), 2)

    def test_legacy_raw_cache_has_unknown_receipt(self):
        self.update()
        raw = next((self.root / 'raw').glob('*.json'))
        data = json.loads(raw.read_text())
        for key in ('received_at', 'validated_at', 'expires_at'):
            data.pop(key)
        raw.write_text(json.dumps(data))
        status = self.update()
        self.assertEqual(status['state'], 'unavailable')
        self.assertIsNone(status['fetched_at'])
        self.assertEqual(self.calls, 1)

    def test_invalid_date_is_unknown(self):
        self.headers['Date'] = 'not a date'
        self.assertEqual(self.update()['state'], 'unavailable')

    def test_display_conversion_cannot_outlive_http_expiry(self):
        from types import SimpleNamespace
        from poecraft.engine.evaluator import _display_rate
        status = self.update()
        prices = SimpleNamespace(live_status=status, live=status['prices'])
        rate = _display_rate(prices, 'Covens')
        self.assertEqual(rate['expires_at'], status['expires_at'])
        self.assertIsNone(_display_rate(prices, 'Standard'))
        self.now += 45
        status = N.load_status(self.root / 'snapshot.json', 'Covens')
        prices.live_status = status
        self.assertIsNone(_display_rate(prices, 'Covens'))

    def test_trade_indexed_is_preserved_not_invented(self):
        from types import SimpleNamespace
        from poecraft.engine.evaluator import _listing
        prices = SimpleNamespace()
        for indexed in (None, 'invalid', '2026-11-15T05:25:16Z'):
            row = _listing(prices, {'listing': {'indexed': indexed}})
            self.assertEqual(row['indexed'], indexed)
