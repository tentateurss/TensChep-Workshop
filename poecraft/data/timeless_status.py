"""Read-only validation of Timeless provenance and file integrity."""
import hashlib
import json
import re
from pathlib import Path

REQUIRED=('index.json','legion.json','tree.json','conquerors.json')+tuple(f'lut_{i}.bin' for i in range(1,7))
_last=None


def status(root=None, game_version=None):
    from . import timeless as data
    from .. import config
    global _last
    root=Path(root or data.TIMELESS_DIR)
    if game_version is None:
        try:game_version=json.loads((config.RAW_DIR/'manifest.json').read_text(encoding='utf-8')).get('game_version')
        except (OSError,ValueError):pass
    result={'compatibility':'unverified','game_version':game_version,'table_game_version':None,'revision':None,'reason':'version_unknown'}
    try:
        meta=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
        if not isinstance(meta,dict) or any(
                key in meta and not isinstance(meta[key],dict)
                for key in ('revisions','sources','sha256')):
            raise ValueError('Invalid manifest')
        hashes={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in REQUIRED}
    except FileNotFoundError:
        return dict(result,compatibility='missing',reason='missing_files')
    except (OSError,ValueError,TypeError):
        return dict(result,compatibility='incompatible',reason='invalid_manifest')
    fingerprint=hashlib.sha256(json.dumps([str(root),meta,hashes,game_version],sort_keys=True).encode()).hexdigest()
    if _last!=fingerprint:
        data._clear()
        _last=fingerprint
    result.update(fingerprint=fingerprint,revision=meta.get('revisions',{}).get('pob'),table_game_version=meta.get('game_version'),sources=meta.get('sources',{}),fetched_at=meta.get('fetched_at'),sha256=hashes,revisions=meta.get('revisions',{}))
    try:
        tree=json.loads((root/'tree.json').read_text(encoding='utf-8'))
        index=json.loads((root/'index.json').read_text(encoding='utf-8'))
        notables={key for key,node in tree['nodes'].items() if node['kind']=='notable'}
        if notables - set(index['nodes']):
            return dict(result,compatibility='incompatible',reason='tree_mismatch')
    except (OSError,ValueError,KeyError,TypeError):
        return dict(result,compatibility='incompatible',reason='invalid_manifest')
    if game_version and str(game_version).startswith('3.30'):
        try:
            legion=json.loads((root/'legion.json').read_text(encoding='utf-8'))
            text=' '.join(' '.join(node.get('sd',[])) for node in legion['nodes'] if node.get('name') in ('Starlight Swig','Regenerative Runes','Celestial Mathematics'))
            legacy=('15% chance for Ward to be Restored when you Use a Flask',
                    'Iron Flasks gain 2 charges when your Ward Breaks',
                    'Added Cold Damage equal to 25% of Ward')
            if any(line in text for line in legacy):
                return dict(result,compatibility='incompatible',reason='legacy_heroic')
        except (OSError,ValueError,KeyError,TypeError):
            return dict(result,compatibility='incompatible',reason='invalid_manifest')
    expected=meta.get('sha256',{})
    if not isinstance(expected,dict):
        return dict(result,compatibility='incompatible',reason='invalid_manifest')
    if any(name in expected and expected[name]!=value for name,value in hashes.items()):
        return dict(result,compatibility='incompatible',reason='checksum_mismatch')
    table_version=meta.get('game_version');tree_version=meta.get('tree_game_version')
    if game_version and any(v and v!=game_version for v in (table_version,tree_version)):
        return dict(result,compatibility='incompatible',reason='version_mismatch')
    if game_version and table_version==tree_version==game_version and all(name in expected for name in REQUIRED) and re.fullmatch('[a-f0-9]{40}',str(result['revision'] or '')) and re.fullmatch('[a-f0-9]{40}',str(meta.get('revisions',{}).get('tree') or '')):
        return dict(result,compatibility='compatible',reason='verified_versions')
    return result


def require():
    from .timeless import TimelessDataMissing
    result=status()
    if result['compatibility'] in ('missing','incompatible'):
        raise TimelessDataMissing(result['reason'])
    return result
