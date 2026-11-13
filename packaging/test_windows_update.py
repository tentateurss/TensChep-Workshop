import json,os,shutil,subprocess,tempfile,time,urllib.request
from pathlib import Path
root=Path.cwd()
def health(version):
 for i in range(60):
  try:
   with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=1) as r:
    if json.load(r)['version'] in (version if isinstance(version,tuple) else (version,)):return True
  except OSError:pass
  time.sleep(.2)
 return False
for fail in [False,True]:
 with tempfile.TemporaryDirectory(prefix='Workshop upgrade ') as td:
  r=Path(td);target=r/'App';work=r/'stage';work.mkdir()
  shutil.copytree(root/'.browser-tools/old-build',target)
  shutil.copytree(root/'dist/TensChep-Workshop',work/'TensChep-Workshop')
  shutil.copytree(root/'dist/TensChep-Workshop',work/'helper')
  data=r/'profile/data';data.mkdir(parents=True);shutil.copyfile(root/'data/poecraft.db',data/'poecraft.db');shutil.copytree(root/'data/raw',data/'raw')
  (data/'templates.json').write_text('personal sentinel')
  env=dict(os.environ,POECRAFT_DATA_DIR=str(data),LOCALAPPDATA=str(r/'profile'),PATH=str(Path(os.environ['SystemRoot'])/'System32'),PYINSTALLER_RESET_ENVIRONMENT='1')
  old=subprocess.Popen([str(target/'TensChep-Workshop.exe'),'serve','--no-browser'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  assert health(('1.0.0-beta.1','1.0.0'))
  if fail:(work/'TensChep-Workshop/TensChep-Workshop.exe').write_bytes(b'invalid executable')
  job={'pid':old.pid,'target':str(target),'new':str(work/'TensChep-Workshop'),'backup':str(r/'backup'),'version':'v1.0.0-beta.2','state':'prepared'}
  jobfile=work/'installation.json';jobfile.write_text(json.dumps(job))
  helper=subprocess.Popen([str(work/'helper/TensChep-Workshop.exe'),'--apply-update',str(jobfile)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  old.terminate();old.wait(timeout=10)
  helper.wait(timeout=70)
  assert health(('1.0.0-beta.1','1.0.0') if fail else '1.0.0-beta.2')
  assert (data/'templates.json').read_text()=='personal sentinel'
  state=json.loads(jobfile.read_text())['state'];assert state==('rolled_back' if fail else 'installed'),state
  # Stop only the test child started from this unique temporary directory.
  subprocess.run(['powershell','-NoProfile','-Command',"Get-CimInstance Win32_Process -Filter \"Name='TensChep-Workshop.exe'\" | Where-Object { $_.ExecutablePath -like '"+str(r)+"*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],check=True)
  time.sleep(.5)
  print('Actual external updater:',state,'user data preserved; PATH without Python')
