"""Versioned PoB passive maps. Never uses the active Timeless tree as a fallback."""
import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from . import config
from .data.timeless import lua_table, node_position

REPOSITORY = 'PathOfBuildingCommunity/PathOfBuilding'
CACHE_FORMAT = 2
_lock = threading.Lock()


class TreeDataError(ValueError):
    pass


def _download(url, limit):
    request = urllib.request.Request(url, headers={'User-Agent': config.user_agent()})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise TreeDataError('invalid')
            return data
    except urllib.error.HTTPError as error:
        raise TreeDataError('missing' if error.code == 404 else 'network') from error
    except OSError as error:
        raise TreeDataError('network') from error


def convert(tree):
    """Only positions and effects present in the versioned source; no cluster generation."""
    nodes, links = {}, set()
    groups = tree['groups']
    if isinstance(groups, list):
        groups = dict(enumerate(groups, 1))
    constants = tree.get('constants') or {}
    radii = constants.get('orbitRadii') or [0, 82, 162, 335, 493]
    counts = constants.get('skillsPerOrbit') or [1, 6, 12, 12, 40]
    entries = dict(enumerate(tree['nodes'], 1)) if isinstance(tree['nodes'], list) else tree['nodes']
    for ident, node in entries.items():
        ident = node.get('skill', ident)
        if not str(ident).isdigit():
            continue
        group = groups.get(node.get('group'))
        if not group or node.get('isProxy') or group.get('isProxy'):
            continue
        x, y = node_position(group, node.get('orbit', 0), node.get('orbitIndex', 0), radii, counts)
        if not all(math.isfinite(n) and abs(n) < 100000 for n in (x, y)):
            raise TreeDataError('invalid')
        if str(ident) in nodes:
            raise TreeDataError('invalid')
        kind = ('start' if 'classStartIndex' in node else 'mastery' if node.get('isMastery') else 'keystone' if node.get('isKeystone') else
                'notable' if node.get('isNotable') else 'socket' if node.get('isJewelSocket') else 'normal')
        nodes[str(ident)] = {'id': str(ident), 'x': round(x, 2), 'y': round(y, 2),
            'group': str(node['group']), 'orbit': node.get('orbit', 0),
            'radius': radii[node.get('orbit', 0)],
            'angle': math.atan2(x-group['x'], group['y']-y),
            'name': node.get('name') or '', 'stats': node.get('stats') or [], 'kind': kind,
            'ascendancy': node.get('ascendancyName'), 'mastery_effects': node.get('masteryEffects') or []}
        for target in node.get('out') or []:
            if str(target) != str(ident):
                links.add(tuple(sorted((str(ident), str(target)))))
    if not 100 < len(nodes) < 10000:
        raise TreeDataError('invalid')
    # PoB separates allocation adjacency from visible connectors (PassiveTree.lua).
    links = [list(pair) for pair in sorted(links) if all(k in nodes for k in pair)
             and all(nodes[k]['kind'] not in ('start', 'mastery') for k in pair)
             and nodes[pair[0]]['ascendancy'] == nodes[pair[1]]['ascendancy']]
    return {'nodes': nodes, 'links': links}


def load_tree(version):
    if not isinstance(version, str) or not re.fullmatch(r'(?:2_6|3_\d{1,2})(?:_ruthless)?(?:_alternate)?', version.replace('.', '_')):
        raise TreeDataError('version')
    version = version.replace('.', '_')
    path = config.DATA_DIR / 'build_trees' / (version + '.json')
    with _lock:
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            expected = f"https://raw.githubusercontent.com/{REPOSITORY}/{cached['revision']}/src/TreeData/{version}/tree.lua"
            if (cached.get('format') == CACHE_FORMAT and cached['version'] == version and cached['nodes'] and
                    re.fullmatch(r'[0-9a-f]{40}', cached['revision']) and cached['source'] == expected):
                return cached
        except (OSError, ValueError, KeyError, TypeError):
            pass
        source_path = f'src/TreeData/{version}/tree.lua'
        try:
            commits = json.loads(_download(f'https://api.github.com/repos/{REPOSITORY}/commits?path={urllib.parse.quote(source_path)}&per_page=1', 1024*1024))
            if not commits:
                raise TreeDataError('missing')
            revision = commits[0]['sha']
            if not re.fullmatch(r'[0-9a-f]{40}', revision):
                raise TreeDataError('invalid')
            url = f'https://raw.githubusercontent.com/{REPOSITORY}/{revision}/{source_path}'
            raw = _download(url, 8*1024*1024)
            # Lua escaped physical newlines are valid strings, but not tokens in the shared data parser.
            payload = convert(lua_table(raw.decode('utf-8').replace('\\\n', '\\n')))
            license_url = f'https://raw.githubusercontent.com/{REPOSITORY}/{revision}/LICENSE.md'
            license_text = _download(license_url, 128*1024).decode('utf-8')
        except TreeDataError:
            raise
        except (ValueError, KeyError, IndexError, TypeError, AttributeError, UnicodeError) as error:
            raise TreeDataError('invalid') from error
        payload.update(format=CACHE_FORMAT, version=version, source=url, revision=revision,
                       sha256=hashlib.sha256(raw).hexdigest(), fetched_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            (path.parent / 'NOTICE.txt').write_text('PoB passive tree data; game data (c) Grinding Gear Games.\n'+license_url+'\n\n'+license_text, encoding='utf-8')
            tmp = path.with_suffix('.tmp')
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            tmp.replace(path)
        except OSError:
            pass  # A read-only cache must not hide successfully downloaded versioned data.
        return payload
