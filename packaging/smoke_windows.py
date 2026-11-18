import json,os,shutil,subprocess,tempfile,time,urllib.request,zipfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='Workshop space ') as td:
 target=Path(td)/'Тест запуска'
 archive=Path(os.environ.get('POECRAFT_TEST_ARCHIVE',root/'dist/TensChep-Workshop-Windows-x64.zip'))
 with zipfile.ZipFile(archive) as z: z.extractall(target)
 exe=target/'TensChep-Workshop/TensChep-Workshop.exe'
 env=dict(os.environ,LOCALAPPDATA=str(target/'profile'),PATH=str(Path(os.environ['SystemRoot'])/'System32'))
 r=subprocess.run([str(exe),'--help'],env=env,cwd=td,capture_output=True,timeout=30)
 assert r.returncode==0,r.stderr
 # Offline integration: copy only a test database into the isolated profile, never into the ZIP.
 data=target/'profile/TensChep Workshop/data';data.mkdir(parents=True,exist_ok=True)
 source=Path(os.environ.get('POECRAFT_TEST_DATA',root/'data'))
 shutil.copyfile(source/'poecraft.db',data/'poecraft.db')
 shutil.copytree(source/'raw',data/'raw')
 for attempt in range(2):
  p=subprocess.Popen([str(exe),'serve','--no-browser','--port','18767'],env=env,cwd=td,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:
   for i in range(60):
    try:
     with urllib.request.urlopen('http://127.0.0.1:18767/api/health',timeout=1) as r: assert json.load(r)['data'];break
    except OSError: time.sleep(.2)
   else: raise AssertionError('server failed')
   for path in ['/','/craft','/evaluator','/build','/build.html','/build.js','/build.css','/app.js','/style.css','/api/locale?lang=ru','/api/meta','/api/timeless/status']:
    with urllib.request.urlopen('http://127.0.0.1:18767'+path,timeout=10) as r: assert r.status==200,path
   request=urllib.request.Request('http://127.0.0.1:18767/api/build/import',
       json.dumps({'source':'<PathOfBuilding><Build className="Witch" targetVersion="3_0"/></PathOfBuilding>'}).encode(),
       {'Content-Type':'application/json','Origin':'http://127.0.0.1:18767','X-Workshop-Build':'1'})
   with urllib.request.urlopen(request,timeout=10) as r:
    imported=json.load(r);assert imported['class']=='Witch' and imported['loadouts']==[]
  finally: p.terminate();p.wait(timeout=10)
 print('EXE smoke OK: isolated PATH, Cyrillic/spaces, resources, API, restart')
