"""Non-blocking release discovery. Does not install or execute downloads."""
import json
import re
import threading
import time
import urllib.request
from . import config, __version__

API = 'https://api.github.com/repos/tentateurss/TensChep-Workshop/releases?per_page=100'
_lock = threading.Lock()
_status = {'state': 'idle', 'release': None, 'channel': 'beta', 'current': __version__}


def version(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?', value)
    if not match:
        raise ValueError('Invalid release version')
    a,b,c,beta = match.groups()
    return (int(a),int(b),int(c),1 if beta is None else 0,int(beta or 0))


def select(releases, current, channel):
    if channel not in ('stable','beta'):
        raise ValueError('Invalid update channel')
    candidates=[]
    for item in releases:
        try:
            tag=item['tag_name'];v=version(tag)
            if item.get('draft') or (channel=='stable' and (item.get('prerelease') or v[3]==0)) or v<=version(current):
                continue
            assets={x['name']:x for x in item.get('assets',[])}
            if 'TensChep-Workshop-Windows-x64.zip' not in assets or 'SHA256SUMS.txt' not in assets:
                continue
            url='https://github.com/tentateurss/TensChep-Workshop/releases/tag/'+tag
            candidates.append((v,{'version':tag,'url':url,'notes':str(item.get('body') or '')[:8000]}))
        except (KeyError,TypeError,ValueError):
            continue
    return max(candidates,key=lambda x:x[0])[1] if candidates else None


def status():
    with _lock:
        return dict(_status)


def check(force=False, channel=None):
    """Start a background check; repeated requests never create parallel fetches."""
    path=config.CONFIG_DIR/'application-updates.json'
    with _lock:
        if _status['state']=='checking':
            return dict(_status)
        try:
            saved=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError):
            saved={}
        chosen=channel or saved.get('channel','beta')
        if chosen not in ('stable','beta'):
            raise ValueError('Invalid update channel')
        _status.update(channel=chosen)
        if not force and saved.get('channel')==chosen and time.time()-saved.get('checked_at',0)<86400:
            _status.update(state=saved.get('state','idle'),release=saved.get('release'),error=saved.get('error'))
            return dict(_status)
        _status.update(state='checking',release=None,error=None)
    def run():
        try:
            req=urllib.request.Request(API,headers={'User-Agent':config.user_agent(),'Accept':'application/vnd.github+json'})
            with urllib.request.urlopen(req,timeout=10) as response:
                releases=json.loads(response.read(4*1024*1024))
            if not isinstance(releases,list):
                raise ValueError('Unexpected GitHub response')
            release=select(releases,__version__,chosen)
            result={'state':'available' if release else 'current','release':release,'error':None}
        except (OSError,ValueError,TypeError) as error:
            result={'state':'error','release':None,'error':str(error)}
        with _lock:
            _status.update(result)
            saved=dict(_status,checked_at=time.time())
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            temp=path.with_suffix('.tmp');temp.write_text(json.dumps(saved),encoding='utf-8');temp.replace(path)
        except OSError:
            pass  # read-only settings never prevent normal application startup
    threading.Thread(target=run,daemon=True).start()
    return status()
