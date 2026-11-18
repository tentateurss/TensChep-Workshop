"""No-argument EXE startup in disposable profiles, without Python in PATH.

Offline by default; enable a real RePoE download with POECRAFT_ONLINE_FIRST_START=1.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import zipfile

root=Path(__file__).resolve().parents[1]
archive=Path(os.environ.get('POECRAFT_TEST_ARCHIVE',root/'dist/TensChep-Workshop-Windows-x64.zip'))
modes=['offline','online'] if os.environ.get('POECRAFT_ONLINE_FIRST_START')=='1' else ['offline']
for mode in modes:
 with tempfile.TemporaryDirectory(prefix='Workshop first start ') as td:
  folder=Path(td)
  with zipfile.ZipFile(archive) as z:z.extractall(folder/'app')
  exe=folder/'app/TensChep-Workshop/TensChep-Workshop.exe'
  profile=folder/'profile'
  env={k:v for k,v in os.environ.items() if not k.startswith('POECRAFT_')}
  env.update(LOCALAPPDATA=str(profile),PATH=str(Path(os.environ['SystemRoot'])/'System32'),
             PYINSTALLER_RESET_ENVIRONMENT='1',PYTHONIOENCODING='utf-8')
  # Suppress the browser only; the no-argument launcher and server run unchanged.
  env['BROWSER']=str(Path(os.environ['SystemRoot'])/'System32/cmd.exe').replace('\\','/')+' /c exit 0'
  data=profile/'TensChep Workshop/data'
  assert not data.exists()
  if mode=='offline':
   env.update(HTTP_PROXY='http://127.0.0.1:9',HTTPS_PROXY='http://127.0.0.1:9',NO_PROXY='')
   p=subprocess.run([str(exe)],env=env,cwd=folder,input=b'\n',capture_output=True,timeout=45)
   text=(p.stdout+p.stderr).decode('utf-8',errors='replace')
   assert p.returncode!=0 and 'Startup failed:' in text and 'Internet is required' in text,text
   assert not (data/'poecraft.db').exists()
   print('First start OFFLINE PASS: clear network failure; no active database; PATH without Python',flush=True)
  else:
   env.pop('HTTP_PROXY',None);env.pop('HTTPS_PROXY',None)
   with (folder/'startup.log').open('wb') as log:
    p=subprocess.Popen([str(exe)],env=env,cwd=folder,stdin=subprocess.PIPE,stdout=log,stderr=subprocess.STDOUT)
    try:
     for _ in range(1200):
      if p.poll() is not None:raise AssertionError((folder/'startup.log').read_text(encoding='utf-8',errors='replace'))
      try:
       with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=1) as response:health=json.load(response)
       if health.get('data') and (data/'poecraft.db').exists():break
      except (OSError,ValueError):pass
      time.sleep(.5)
     else:raise AssertionError('First-start download timed out')
     with urllib.request.urlopen('http://127.0.0.1:8765/api/meta',timeout=15) as response:meta=json.load(response)
     assert meta['version']==health['version'] and meta['data'].get('game_version'),meta
     print('First start ONLINE PASS:',health['version'],'RePoE',meta['data']['game_version'],'fresh database; PATH without Python',flush=True)
    finally:
     p.terminate();p.wait(timeout=15)
