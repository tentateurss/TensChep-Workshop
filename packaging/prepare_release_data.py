"""Build CI data from an immutable, checksummed RePoE export via existing staging."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from poecraft import config


def minified_data(value):
    """Match RePoE's published min export: omit null object fields, keep values."""
    if isinstance(value, dict):
        return {key: minified_data(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [minified_data(item) for item in value]
    return value


def download(lock, folder):
    if lock['repository'] != 'repoe-fork/repoe-fork.github.io' or not re.fullmatch(r'[0-9a-f]{40}', lock['revision']):
        raise ValueError('Invalid immutable RePoE source')
    base = 'https://raw.githubusercontent.com/' + lock['repository'] + '/' + lock['revision'] + '/'
    required = {'version.txt', 'exported-version.txt'} | {'data/' + n + '.json' for n in config.REPOE_FILES}
    if not required.issubset(lock['files']):
        raise ValueError('Incomplete release data lock')
    for name, entry in lock['files'].items():
        if '\\' in name or PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('Invalid snapshot path')
        request = urllib.request.Request(base + name, headers={'User-Agent': config.user_agent()})
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read(entry['bytes'] + 1)
        if len(body) != entry['bytes'] or hashlib.sha256(body).hexdigest() != entry['sha256']:
            raise ValueError('Release data checksum mismatch: ' + name)
        if name.endswith('.json'):
            json.loads(body)
        elif body.decode().strip() != lock['version']:
            raise ValueError('Release data version mismatch: ' + name)
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


def main():
    lock = json.loads((ROOT / 'packaging/repoe-lock.json').read_text())
    with tempfile.TemporaryDirectory(prefix='Workshop pinned export ') as td:
        folder = Path(td)
        download(lock, folder)

        class Snapshot(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/poe1.html':
                    # Staging's index contract; version is from both verified upstream version files.
                    body = ('PoE version ' + lock['version']).encode()
                else:
                    name = 'data/' + self.path.lstrip('/').replace('.min.json', '.json')
                    if name not in lock['files']:
                        self.send_error(404)
                        return
                    body = json.dumps(minified_data(json.loads((folder / name).read_bytes())),
                                      ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Snapshot)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run([sys.executable, '-m', 'poecraft', 'update', '--url',
                            'http://127.0.0.1:' + str(server.server_port),
                            '--expect-version', lock['version']], cwd=ROOT, check=True)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
    # Retain actual immutable URLs and hashes alongside the loopback staging transport.
    for path in [config.MANIFEST_PATH, config.DATA_DIR / 'compatibility.json']:
        value = json.loads(path.read_text(encoding='utf-8'))
        value['release_source'] = lock
        path.write_text(json.dumps(value, indent=2), encoding='utf-8')
    with sqlite3.connect(config.DB_PATH) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('release_source', json.dumps(lock)))
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                   ('compatibility', (config.DATA_DIR / 'compatibility.json').read_text(encoding='utf-8')))
    print('Verified release data:', lock['version'], lock['revision'])


if __name__ == '__main__':
    main()
