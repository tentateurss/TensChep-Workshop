"""Install the candidate ZIP using the published baseline helper, in disposable copies."""
import hashlib,json,os,shutil,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
from poecraft import __version__
from poecraft.installer import extract,ARCHIVE

baseline=Path(os.environ.get('POECRAFT_UPDATE_BASELINE',root/'.browser-tools/old-build'))
source=Path(os.environ.get('POECRAFT_TEST_DATA',root/'data'))
archive=Path(os.environ.get('POECRAFT_TEST_ARCHIVE',root/'dist'/ARCHIVE))
baseline_version=os.environ.get('POECRAFT_BASELINE_VERSION','1.0.0-beta.4')

digest=archive.with_name('SHA256SUMS.txt').read_text().split()[0]

def health(version):
 for _ in range(60):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=1) as response:
    if json.load(response)['version']==version:return True
  except OSError:pass
  time.sleep(.2)
 return False

for fail in [False,True]:
 with tempfile.TemporaryDirectory(prefix='Workshop upgrade ') as td:
  r=Path(td);target=r/'App';work=r/'stage';work.mkdir()
  shutil.copytree(baseline,target)
  new=extract(archive,digest,work)
  shutil.copytree(baseline,work/'helper')
  profile=r/'profile';data=profile/'TensChep Workshop/data';data.mkdir(parents=True)
  shutil.copyfile(source/'poecraft.db',data/'poecraft.db');shutil.copytree(source/'raw',data/'raw')
  (data/'templates.json').write_text('personal sentinel')
  config=data.parent/'config';config.mkdir();(config/'preferences.json').write_text('{"language":"ru","sentinel":true}')
  protected=[data/'poecraft.db',data/'templates.json',config/'preferences.json']
  hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
  env=dict(os.environ,POECRAFT_DATA_DIR=str(data),POECRAFT_CONFIG_DIR=str(config),LOCALAPPDATA=str(profile),PATH=str(Path(os.environ['SystemRoot'])/'System32'),PYINSTALLER_RESET_ENVIRONMENT='1')
  try:
   old=subprocess.Popen([str(target/'TensChep-Workshop.exe'),'serve','--no-browser'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   assert health(baseline_version),'Published baseline did not start'
   if fail:(new/'TensChep-Workshop.exe').write_bytes(b'invalid executable')
   job={'pid':old.pid,'target':str(target),'new':str(new),'backup':str(r/'backup'),'version':'v'+__version__,'state':'prepared'}
   jobfile=work/'installation.json';jobfile.write_text(json.dumps(job))
   helper=subprocess.Popen([str(work/'helper/TensChep-Workshop.exe'),'--apply-update',str(jobfile)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
   old.terminate();old.wait(timeout=10);helper.wait(timeout=70)
   assert health(baseline_version if fail else __version__),'Installed or restored server failed'
   for p,h in hashes.items():assert hashlib.sha256(p.read_bytes()).hexdigest()==h,str(p)
   state=json.loads(jobfile.read_text())['state'];assert state==('rolled_back' if fail else 'installed'),state
   for page in ['/','/craft','/evaluator','/app.js','/api/meta']:
    with urllib.request.urlopen('http://127.0.0.1:8765'+page,timeout=15) as response:assert response.status==200,page
   print(baseline_version,'->',__version__,state,'SHA-256 verified; database/settings preserved; PATH without Python',flush=True)
  finally:
   # Stop only children whose executable is inside this unique disposable directory.
   subprocess.run(['powershell','-NoProfile','-Command',"Get-CimInstance Win32_Process -Filter \"Name='TensChep-Workshop.exe'\" | Where-Object { $_.ExecutablePath -like '"+str(r)+"*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],check=True)
   time.sleep(.5)
