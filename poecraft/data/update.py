"""Staged updates: active data is never downloaded over in place."""
from __future__ import annotations
import json
from contextlib import closing
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from .. import config


def source_version(url):
    req = urllib.request.Request(url.rstrip('/') + '/poe1.html', headers={'User-Agent': config.user_agent()})
    with urllib.request.urlopen(req, timeout=30) as response:
        text = response.read().decode('utf-8')
    match = re.search(r'PoE version\s+(\d+\.\d+\.\d+(?:\.\d+)*)', text)
    if not match:
        raise ValueError('Source game version is unverified; active data has not been changed')
    return match.group(1)


def update(url, expected=None, dry_run=False):
    version = source_version(url)
    if expected and not (version == expected or version.startswith(expected + '.')):
        raise ValueError(f'WAITING FOR GAME DATA: requested {expected}, source exports {version}')
    print(f'Source game version: {version}')
    if dry_run:
        return
    active = config.DATA_DIR.resolve()
    active.parent.mkdir(parents=True, exist_ok=True)
    # Stage alongside the active directory, so promotion stays on the same filesystem.
    stage = Path(tempfile.mkdtemp(prefix='.poecraft-stage-', dir=active.parent))
    backup = active.with_name(active.name + '.backup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + stage.name[-6:])
    try:
        if active.exists():
            shutil.copytree(active, stage, dirs_exist_ok=True)
        from .compatibility import quarantine_inherited, inventory
        quarantine_inherited(stage)
        env = dict(os.environ, POECRAFT_DATA_DIR=str(stage), POECRAFT_STAGED_UPDATE='1')
        subprocess.run([sys.executable, '-m', 'poecraft', 'update', '--url', url], env=env, check=True)
        if source_version(url) != version:
            raise ValueError('Source changed during download; retry the update')
        with closing(sqlite3.connect(stage / 'poecraft.db')) as db:
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('SQLite integrity check failed')
            for table in ('bases', 'mods', 'essences', 'fossils', 'bench_crafts'):
                if not db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]:
                    raise ValueError(f'Empty game table: {table}')
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('game_version', version))
            db.commit()
        manifest_path = stage / 'raw' / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        manifest['game_version'] = version
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        report = inventory(stage, version, url)
        with closing(sqlite3.connect(stage / "poecraft.db")) as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("compatibility", json.dumps(report)))
            db.commit()
        if active.exists():
            active.rename(backup)
        try:
            stage.rename(active)
        except BaseException:
            if backup.exists():
                backup.rename(active)
            raise
        print(f'Updated game data: {version}. Backup: {backup}. Restart the server.')
    finally:
        # Preserve failed staging artifacts for diagnosis; never remove user data.
        if stage.exists():
            print(f'Active data unchanged. Staging retained: {stage}')
