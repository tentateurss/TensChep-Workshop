"""Explicit equipment searches using the existing Evaluator and Trade limits."""
import re
import urllib.parse
import json
import math
import hashlib
import threading
from concurrent.futures import Future
from .engine import evaluator
from .engine.context import generic_text
from .engine.itemtext import _clean, _marker, SKIP_LINE
from .data import trade

_lock = threading.Lock()
_flights = {}


class BuildTradeError(ValueError):
    pass


def item_text(item):
    raw = item.get('text')
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > 65536:
        raise BuildTradeError('item')
    # These require PoB's variant/roll resolver; never search the ordinary variant silently.
    mod_text = '\n'.join(line for line in raw.splitlines() if not re.match(r'^\s*(?:Prefix|Suffix):',line))
    if re.search(r'\{(?:variant|version|group|range):|(?<![\d.])\(-?\d+(?:\.\d+)?--?\d+|^(?:Variant|Has Variants|Selected Variant):', mod_text, re.M):
        raise BuildTradeError('variant')
    lines = []
    implicits = 0
    for line in raw.splitlines():
        line = line.strip()
        if re.fullmatch(r'Implicits:\s*\d+', line):
            implicits = int(line.split(':')[1])
            continue
        if re.match(r'^(?:Unique ID|League|LevelReq|Crafted|Prefix|Suffix|Variant|Selected .+ Variant|Has .+ Variant):', line):
            continue
        line = line.replace('{crafted}', '').replace('{fractured}', '') + (
            ' (crafted)' if '{crafted}' in line else ' (fractured)' if '{fractured}' in line else '')
        if implicits and line:
            line = re.sub(r' \((?:crafted|fractured)\)$','',line) + ' (implicit)'
            implicits -= 1
        if line:
            lines.append(line)
    if any(line.startswith('--------') for line in lines):
        return '\n'.join(lines)
    if not lines or not re.match(r'^(?:Rarity|Редкость|Seltenheit):', lines[0], re.I):
        raise BuildTradeError('item')
    header = 1
    named = re.search(r'unique|rare|уник|редк|einzigartig|selten',lines[0].split(':',1)[1],re.I)
    while header < min(3 if named else 2,len(lines)) and ':' not in lines[header]:
        header += 1
    return '\n'.join(lines[:header]+['--------']+lines[header:])


def prepare(item, *, refresh=False, index=None):
    """Inspect a bounded PoB item without searching or fetching listings."""
    text = item_text(item)
    h = evaluator._header(text)
    rarity = next((key for key, words in {
        'unique': ('unique', 'уник', 'einzigartig'), 'rare': ('rare', 'редк', 'selten'),
        'magic': ('magic', 'волшеб', 'magisch'), 'normal': ('normal', 'обыч', 'норм')
    }.items() if any(word in h['rarity'] for word in words)), None)
    if rarity is None:
        raise BuildTradeError('item')
    names = evaluator._all_names()
    base = next((names[evaluator._norm(line)] for line in h['head'] if evaluator._norm(line) in names), None)
    base = base or evaluator._contained_base(h['head'], names)
    if base is None:
        raise BuildTradeError('item')
    language = 'ru' if re.search('[А-Яа-яЁё]', h['head'][0]) else 'de' if text.startswith('Seltenheit:') else 'en'
    if index is None and rarity == 'normal':
        index = {}
    if index is None:
        if refresh:
            body = trade._get('/api/trade/data/stats','stats',trade.STATS_TTL,language)
            if not isinstance(body,dict) or not isinstance(body.get('result'),list):
                raise BuildTradeError('catalogue')
            index = trade.stat_index(language,body=body)
        else:
            try:
                name = 'stats' + ('' if language == 'en' else '_' + language)
                body = json.loads(trade._cache(name).read_text(encoding='utf-8'))['body']
                if not isinstance(body,dict) or not isinstance(body.get('result'),list):
                    raise ValueError('Invalid cached catalogue')
                index = trade.stat_index(language, body=body)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                raise BuildTradeError('catalogue') from None
    query = trade.build_query(base[0], [], rarity)
    query['query']['filters']['misc_filters'] = {'filters': evaluator._status_filters(text)}
    if rarity == 'unique':
        query['query']['name'] = h['head'][0]
        if language != 'en':
            query['query']['type'] = evaluator.i18n.game_text('base', base[2], base[0], language)[0]
    else:
        language = 'en'  # non-unique names are not filters; base names are canonical English
    controls, skipped = [], []

    def add(line, options, value, *, kind='stat', field=None):
        ident = hashlib.sha256((kind+'|'+line).encode()).hexdigest()
        minimum = round(value * .8, 2) if value is not None else None
        if value is not None and abs(value) <= 1:
            minimum = value
        controls.append({'id': ident, 'label': line, 'options': options, 'choice': options[0]['id'],
                         'enabled': True, 'min': minimum, 'max': None, 'value': value,
                         'kind': kind, 'field': field})

    for line in h['body']:
        clean, kind = _marker(_clean(line))
        if ':' in clean:
            prop = re.fullmatch(r'(Armour|Evasion Rating|Energy Shield):\s*(\d+)', clean)
            if prop:
                field = {'Armour':'ar', 'Evasion Rating':'ev', 'Energy Shield':'es'}[prop[1]]
                add(line, [{'id':field, 'label':'build.trade.defence.'+field}], float(prop[2]), kind='defence', field=field)
                controls[-1]['reason'] = 'build.trade.defence.normalized'
            elif not re.match(r'^(?:Item Level|LevelReq|EnergyShieldBasePercentile|ArmourBasePercentile|EvasionBasePercentile|Sockets):',clean):
                skipped.append({'line':line, 'reason':'build.trade.unmapped'})
            continue
        if SKIP_LINE.match(clean) or clean.startswith('{'):
            continue
        numbers = re.findall(r'-?\d+(?:\.\d+)?', clean)
        generic = generic_text(clean)
        ids = trade.trade_stat_ids(index, kind, generic, False)
        if len(ids) != 1 or len(numbers) > 1:
            skipped.append({'line':line, 'reason':'build.trade.unmapped'})
            continue
        options = [{'id':ids[0], 'label':kind}]
        local_id = index.get(kind,{}).get(generic+' (Local)')
        ambiguous = local_id is not None and local_id != ids[0]
        if ambiguous:
            options[0]['label'] = generic
            options.append({'id':local_id,'label':generic+' (Local)'})
        pseudo = {'+# to maximum Life':'pseudo.pseudo_total_life',
                  '+#% to Chaos Resistance':'pseudo.pseudo_total_chaos_resistance'}.get(generic)
        if pseudo and pseudo in index.get('pseudo', {}).values():
            options.append({'id':pseudo, 'label':next(k for k,v in index['pseudo'].items() if v==pseudo)})
        add(line, options, float(numbers[0]) if numbers else None)
        if ambiguous:
            controls[-1].update(enabled=False,reason='build.trade.ambiguous')
    _, sockets, _, _ = evaluator._unique_properties(h, False, index=index)
    for field, bounds in sockets.items():
        label = 'build.trade.' + field
        add(label, [{'id':field, 'label':label}], bounds['min'], kind='socket', field=field)
        controls[-1].update(min=bounds['min'],max=bounds.get('max'))
    return {'query':query, 'filters':controls, 'skipped':skipped, 'rarity':rarity,
            'name':h['head'][0], 'base':base[0], 'language':language}


def payload(preview, settings=None, status='online'):
    if status not in ('online', 'any'):
        raise BuildTradeError('filters')
    result = json.loads(json.dumps(preview['query']))
    result['query']['status']['option'] = status
    rows = preview['filters']
    if settings is not None:
        if not isinstance(settings, list) or len(settings) != len(rows):
            raise BuildTradeError('filters')
        chosen = {row.get('id'):row for row in settings if isinstance(row,dict)}
        if len(chosen) != len(rows):
            raise BuildTradeError('filters')
        rows = [dict(row, **{k:chosen.get(row['id'],{}).get(k) for k in ('enabled','choice','min','max')}) for row in rows]
    stats = []
    for row in rows:
        if type(row['enabled']) is not bool or row['choice'] not in [o['id'] for o in row['options']]:
            raise BuildTradeError('filters')
        bounds = {k:row[k] for k in ('min','max') if row[k] is not None}
        if any(type(v) not in (int,float) or not math.isfinite(v) or abs(v)>1e9 for v in bounds.values()):
            raise BuildTradeError('filters')
        if row['value'] is None and bounds:
            raise BuildTradeError('filters')
        if 'min' in bounds and 'max' in bounds and bounds['min']>bounds['max']:
            raise BuildTradeError('filters')
        if row['kind']=='socket' and any(v<0 or v>6 or v!=int(v) for v in bounds.values()):
            raise BuildTradeError('filters')
        if not row['enabled']:
            continue
        if row['kind'] in ('defence','socket'):
            group = 'armour_filters' if row['kind']=='defence' else 'socket_filters'
            result['query']['filters'].setdefault(group,{'filters':{}})['filters'][row['field']]=bounds
        else:
            stats.append({'id':row['choice'], **({'value':bounds} if bounds else {})})
    if preview['rarity'] in ('rare','magic') and not stats and not any(result['query']['filters'].get(k) for k in ('armour_filters','socket_filters')):
        raise BuildTradeError('no_filters')
    result['query']['stats'] = trade.stats_groups(stats)
    return result


def search(item, league, settings=None, status='online', expected=None):
    if not isinstance(league,str) or not league or len(league)>100:
        raise BuildTradeError('league')
    preview = prepare(item)
    query = payload(preview, settings, status)
    if expected is not None and expected != query:
        raise BuildTradeError('changed')
    key = (league, preview['language'], json.dumps(query,sort_keys=True))
    with _lock:
        flight = _flights.get(key)
        owner = flight is None
        if owner:
            flight = _flights[key] = Future()
    try:
        if owner:
            try:
                with trade.request_reason('rare_manual'):
                    found = trade.search_ids(league, query, language=preview['language'])
                flight.set_result(found)
            except BaseException as error:
                flight.set_exception(error)
                raise
        else:
            found = flight.result(timeout=trade.TIMEOUT+30)
    finally:
        if owner:
            with _lock:
                _flights.pop(key,None)
    if not found.get('id') and not found.get('inferred'):
        raise BuildTradeError('unavailable')
    url = (trade.search_url(league,found['id'],preview['language']) if found.get('id') else
           trade.long_url(league,query).replace(trade.SITE,trade._site(preview['language']),1))
    parsed=urllib.parse.urlsplit(url or '')
    base_path = '/trade/search/'+urllib.parse.quote(league,safe='')
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or parsed.hostname not in ('www.pathofexile.com','ru.pathofexile.com','de.pathofexile.com')
            or not (parsed.path.startswith(base_path+'/') or
                    parsed.path == base_path and urllib.parse.parse_qs(parsed.query).get('q'))):
        raise BuildTradeError('unavailable')
    return {'url':url,'notes':[],'league':league,'query':query,'skipped':preview['skipped'],
            'status':status,'total':found.get('total'), 'inferred':found.get('inferred',False),
            'state':'found' if found.get('total') else 'no_online_matches' if status=='online' else 'no_matches'}
