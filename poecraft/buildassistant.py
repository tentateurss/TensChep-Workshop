"""Read-only PoE 1 PoB import. No game database, build execution or calculations."""
import base64
import binascii
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib

MAX_INPUT = 1024 * 1024
MAX_XML = 4 * 1024 * 1024
CLASSES = {'Scion', 'Marauder', 'Ranger', 'Witch', 'Duelist', 'Templar', 'Shadow'}


class BuildImportError(ValueError):
    """Stable error code; transport details stay in the server log."""


class _XML(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = self.count = 0

    def doctype(self, *args):
        raise BuildImportError('unsafe_xml')

    def start(self, tag, attrs):
        self.depth += 1
        self.count += 1
        if self.depth > 64 or self.count > 50000:
            raise BuildImportError('too_large')
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BuildImportError('redirect')


def _download(source):
    # Build the request from a validated ID, never from an arbitrary user URL.
    if not re.fullmatch(r'https://pobb\.in/[A-Za-z0-9_-]{4,80}/?', source):
        raise BuildImportError('url')
    ident = source.rstrip('/').rsplit('/', 1)[1]
    request = urllib.request.Request(f'https://pobb.in/{ident}/raw', headers={
        'User-Agent': 'TensChep-Workshop PoB import', 'Accept': 'text/plain', 'Accept-Encoding': 'identity'})
    start = time.monotonic()
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=10) as response:
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise BuildImportError('format')
            size = response.headers.get('Content-Length')
            if size and int(size) > MAX_INPUT:
                raise BuildImportError('too_large')
            chunks, total = [], 0
            while True:
                chunk = response.read(min(65536, MAX_INPUT + 1 - total))
                total += len(chunk)
                if total > MAX_INPUT:
                    raise BuildImportError('too_large')
                if time.monotonic() - start > 20:
                    raise BuildImportError('network')
                if not chunk:
                    return b''.join(chunks)
                chunks.append(chunk)
    except BuildImportError:
        raise
    except (OSError, ValueError) as error:
        raise BuildImportError('network') from error


def _integer(value):
    if value in (None, '', 'nil'):
        return None
    if not re.fullmatch(r'\d{1,16}', value) or int(value) > 2**53 - 1:
        raise BuildImportError('format')
    return int(value)


def _flag(value):
    return {'true': True, 'false': False}.get(value)


def _tree_url(value):
    value = (value or '').strip()
    if re.fullmatch(r'https://(?:www\.)?pathofexile\.com/passive-skill-tree/[A-Za-z0-9_=-]+/?', value):
        return value
    return None


def _loadouts(trees, skills, items, configs):
    """PoB SyncLoadouts conventions; duplicate matches stay independent."""
    ambiguous = False

    def links(entry):
        title = entry.get('title')
        if not title:
            return 'name', [], None
        marker = re.search(r'\{([A-Za-z0-9,]+)\}', title)
        if marker:
            keys = list(dict.fromkeys(k for k in marker[1].split(',') if k))
            return 'identifier', keys, title.replace(marker[0], '').strip() or None
        return 'name', [title], title

    def index(entries):
        nonlocal ambiguous
        result = {}
        for entry in entries:
            mode, keys, _ = links(entry)
            for key in keys:
                lookup = (mode, key)
                if lookup in result:
                    ambiguous = True
                    result[lookup] = None
                else:
                    result[lookup] = entry
        return result

    indexes = [index(entries) for entries in (trees, skills, items, configs)]
    result = []
    for tree in trees:
        mode, keys, label = links(tree)
        for key in keys:
            if indexes[0].get((mode, key)) is not tree:
                continue
            matches = [entries[0] if len(entries) == 1 else lookup.get((mode, key))
                       for entries, lookup in zip((skills, items, configs), indexes[1:])]
            if not matches[0] or not matches[1] or (configs and not matches[2]):
                continue
            result.append({'id': mode + ':' + key, 'title': (label or '') + (' {' + key + '}' if mode == 'identifier' else ''),
                           'tree': tree['id'], 'skills': matches[0]['id'], 'items': matches[1]['id'],
                           'config': matches[2]['id'] if matches[2] else None, 'association': mode})
            if len(result) > 10000:
                raise BuildImportError('too_large')
    return result, ambiguous


def import_build(source):
    """Accept an XML document, a Base64/zlib export or an HTTPS pobb.in link."""
    if not isinstance(source, str):
        raise BuildImportError('format')
    if len(source.encode('utf-8')) > MAX_INPUT:
        raise BuildImportError('too_large')
    source = source.lstrip('\ufeff').strip()
    origin = None
    if source.startswith(('https:', 'http:')):
        origin = source
        raw = _download(source)
    else:
        raw = source.encode('utf-8')
    try:
        if not raw.lstrip().startswith(b'<'):
            code = re.sub(rb'\s+', b'', raw)
            compressed = base64.b64decode(code + b'=' * (-len(code) % 4), altchars=b'-_', validate=True)
            decoder = zlib.decompressobj()
            raw = decoder.decompress(compressed, MAX_XML + 1)
            if len(raw) > MAX_XML or decoder.unconsumed_tail:
                raise BuildImportError('too_large')
            if not decoder.eof or decoder.unused_data:
                raise BuildImportError('format')
        if not raw or len(raw) > MAX_XML:
            raise BuildImportError('too_large' if raw else 'format')
        text = raw.decode('utf-8-sig')
        if '\x00' in text:
            raise BuildImportError('unsafe_xml')
        root = ET.fromstring(text, parser=ET.XMLParser(target=_XML()))
    except (binascii.Error, zlib.error, UnicodeError, ET.ParseError) as error:
        raise BuildImportError('format') from error
    build = root.find('Build')
    if root.tag == 'PathOfBuilding2' or (build is not None and build.get('targetVersion', '').startswith('0_')):
        raise BuildImportError('poe2')
    if root.tag != 'PathOfBuilding' or build is None:
        raise BuildImportError('format')
    if build.get('targetVersion') not in (None, '3_0', '2_6'):
        raise BuildImportError('version')
    if build.get('className') and build.get('className') not in CLASSES:
        raise BuildImportError('version')
    trees = []
    tree = root.find('Tree')
    for index, spec in enumerate(root.findall('Tree/Spec'), 1):
        version = spec.get('treeVersion')
        if version and not re.fullmatch(r'[23][_.]\d+(?:[_.]\d+)*(?:_ruthless)?(?:_alternate)?', version):
            raise BuildImportError('version')
        nodes = spec.get('nodes')
        nodes = [_integer(n.strip()) for n in nodes.split(',')] if nodes else ([] if nodes == '' else None)
        if nodes is not None and (None in nodes or len(nodes) > 10000):
            raise BuildImportError('format')
        trees.append({'id': str(index), 'title': spec.get('title'), 'version': version,
                      'class_id': _integer(spec.get('classId')), 'ascendancy_id': _integer(spec.get('ascendClassId')),
                      'nodes': nodes, 'mastery_effects': spec.get('masteryEffects'),
                      'overrides': [dict(n.attrib) for n in spec.findall('Overrides/Override')],
                      'edited_nodes': [dict(n.attrib) for n in spec.findall('EditedNodes/*')],
                      'url': _tree_url(spec.findtext('URL')),
                      'sockets': [dict(s.attrib) for s in spec.findall('Sockets/Socket')],
                      'active': tree.get('activeSpec') == str(index)})
    skills = root.find('Skills')
    skill_sets = []
    if skills is not None:
        sets = list(skills.findall('SkillSet'))
        if skills.findall('Skill'):
            sets.insert(0, skills)
        for section in sets:
            groups = []
            for group in section.findall('Skill'):
                gems = [{'name': g.get('nameSpec'), 'skill_id': g.get('skillId'), 'gem_id': g.get('gemId'),
                         'level': _integer(g.get('level')), 'quality': _integer(g.get('quality')),
                         'enabled': _flag(g.get('enabled'))} for g in group.findall('Gem')]
                groups.append({'label': group.get('label'), 'slot': group.get('slot'),
                               'enabled': _flag(group.get('enabled')), 'main_active_skill': _integer(group.get('mainActiveSkill')),
                               'gems': gems})
            ident = section.get('id', 'legacy')
            skill_sets.append({'id': ident, 'title': section.get('title'), 'groups': groups,
                               'active': skills.get('activeSkillSet') == ident or (section is skills and len(sets) == 1)})
    items = root.find('Items')
    inventory, item_sets = {}, []
    if items is not None:
        for item in items.findall('Item'):
            ident = item.get('id')
            if not ident or ident in inventory:
                raise BuildImportError('format')
            raw_item = (item.text or '').strip()
            lines = raw_item.splitlines()
            name = lines[1].strip() if len(lines) > 1 and lines[0].startswith('Rarity:') else None
            inventory[ident] = {'id': ident, 'name': name, 'text': raw_item, 'variants': dict(item.attrib),
                                'mod_ranges': [dict(m.attrib) for m in item.findall('ModRange')]}
        sets = list(items.findall('ItemSet'))
        if items.findall('Slot'):
            sets.insert(0, items)
        for section in sets:
            slots = [{'name': s.get('name'), 'item_id': s.get('itemId')}
                     for s in section.findall('Slot')]
            ident = section.get('id', 'legacy')
            item_sets.append({'id': ident, 'title': section.get('title'), 'slots': slots,
                              'second_weapon_set': _flag(section.get('useSecondWeaponSet', items.get('useSecondWeaponSet'))),
                              'active': items.get('activeItemSet') == ident or (section is items and len(sets) == 1)})
    config = root.find('Config')
    config_sets = [{'id': c.get('id'), 'title': c.get('title')}
                   for c in root.findall('Config/ConfigSet')]
    if config is not None and not config_sets:
        config_sets = [{'id': 'legacy', 'title': None}]
    for sets in (skill_sets, item_sets, config_sets):
        identifiers = [s['id'] for s in sets]
        if any(not ident for ident in identifiers) or len(set(identifiers)) != len(identifiers):
            raise BuildImportError('format')
    loadouts, ambiguous = _loadouts(trees, skill_sets, item_sets, config_sets)
    warnings = ['import_only', 'linked_sets' if loadouts else 'separate_sets', 'tree_view']
    if ambiguous:
        warnings.append('ambiguous_sets')
    if any(s['item_id'] not in (None, '0', '') and s['item_id'] not in inventory for group in item_sets for s in group['slots']):
        warnings.append('missing_items')
    return {'game': 'poe1', 'class': build.get('className'), 'ascendancy': build.get('ascendClassName'),
            'level': _integer(build.get('level')), 'format_version': build.get('targetVersion'),
            'main_socket_group': _integer(build.get('mainSocketGroup')), 'trees': trees,
            'skill_sets': skill_sets, 'item_sets': item_sets, 'inventory': list(inventory.values()),
            'loadouts': loadouts,
            'warnings': warnings, 'source': origin, 'sha256': hashlib.sha256(raw).hexdigest()}
