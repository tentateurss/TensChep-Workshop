"""Compatibility inventory for staged game exports."""
import json
import uuid
from pathlib import Path
from .fetch import _sha256
from . import locales
from .. import config


def quarantine_inherited(stage):
    archive = stage / 'inherited-data' / uuid.uuid4().hex
    paths = [stage / f'locale_game_{code}.json' for code in config.LOCALE_LANGUAGES]
    paths += [stage / 'raw' / language for language in config.LOCALE_LANGUAGES.values()]
    paths += list(stage.glob('*_cache.json'))
    for path in paths:
        if path.exists():
            target = archive / path.relative_to(stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                # Previous quarantine already preserved in active data and its backup.
                continue
            path.rename(target)


def inventory(stage, version, url):
    raw = stage / 'raw'
    manifest = json.loads((raw / 'manifest.json').read_text(encoding='utf-8'))
    sources = {}
    for name in config.REPOE_FILES:
        path = raw / (name + '.json')
        digest = _sha256(path)
        if digest != manifest['files'][name]['sha256']:
            raise ValueError(f'Mandatory source checksum mismatch: {name}')
        if not json.loads(path.read_text(encoding='utf-8')):
            raise ValueError(f'Empty mandatory source: {name}')
        sources[name] = {'required': True, 'version': version, 'url': url.rstrip('/') + '/' + name + '.min.json',
                         'sha256': digest, 'compatibility': 'unverified',
                         'validation': 'JSON and checksum valid; version declared by export index, not embedded in file'}
    for code, language in config.LOCALE_LANGUAGES.items():
        output = stage / f'locale_game_{code}.json'
        files = [raw / language / (name + '.json') for name in locales.FILES]
        entry = {'required': False, 'version': None, 'url': url.rstrip('/') + '/' + language + '/',
                 'sha256': {p.name: _sha256(p) for p in files if p.exists()}}
        try:
            locales.build(code, language, raw, stage)
            entry.update(compatibility='compatible', validation='IDs and non-localized fields match mandatory export')
        except FileNotFoundError:
            entry.update(compatibility='missing', validation='Incomplete localized source; English fallback')
        except (ValueError, KeyError, TypeError) as error:
            entry.update(compatibility='incompatible', validation=str(error))
        if entry['compatibility'] != 'compatible' and output.exists():
            target = stage / 'rejected-locales' / output.name
            target.parent.mkdir(exist_ok=True)
            output.replace(target)
        sources['locale_' + code] = entry
    timeless = stage / 'timeless'
    from .timeless_status import status
    sources['timeless'] = dict(status(timeless, version), required=False,
        sha256={p.name: _sha256(p) for p in timeless.glob('*') if p.is_file()})
    report = {'game_version': version, 'sources': sources, 'fully_compatible': False}
    (stage / 'compatibility.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report
