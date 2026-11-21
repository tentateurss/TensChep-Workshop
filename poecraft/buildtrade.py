"""Explicit equipment searches using the existing Evaluator and Trade limits."""
import re
import urllib.parse
from .engine import evaluator


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
    named = re.search(r'unique|rare|уник|редк|einzigartig|selten',lines[0],re.I)
    while header < min(3 if named else 2,len(lines)) and ':' not in lines[header]:
        header += 1
    return '\n'.join(lines[:header]+['--------']+lines[header:])


def search(item, league):
    if not isinstance(league,str) or not league or len(league)>100:
        raise BuildTradeError('league')
    text = item_text(item)
    result = evaluator.evaluate(text, league=league, load_listings=True)
    notes = result.get('notes', [])
    if any(n.get('level')=='error' for n in notes):
        return {'url':None,'notes':notes,'league':league}
    url=result.get('search_url')
    parsed=urllib.parse.urlsplit(url or '')
    if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.port not in (None,443)
            or parsed.hostname not in ('www.pathofexile.com','ru.pathofexile.com','de.pathofexile.com')
            or not parsed.path.startswith('/trade/search/'+urllib.parse.quote(league,safe='')+'/')):
        raise BuildTradeError('unavailable')
    return {'url':url,'notes':notes,'league':league,
            'dropped':(result.get('passes') or [{}])[-1].get('dropped',[])}
