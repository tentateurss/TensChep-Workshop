"""Verified portable update staging and external transactional installation."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
import uuid

ARCHIVE='TensChep-Workshop-Windows-x64.zip'
FOLDER='TensChep-Workshop'
EXE='TensChep-Workshop.exe'
REQUIRED=[EXE, '_internal/base_library.zip', '_internal/python313.dll',
          '_internal/poecraft/web/hub.html', '_internal/poecraft/web/index.html',
          '_internal/poecraft/web/evaluator.html', '_internal/poecraft/web/updates.js',
          '_internal/poecraft/locales/en.json', '_internal/poecraft/locales/ru.json',
          '_internal/poecraft/locales/de.json', '_internal/poecraft/rules/mechanics.json',
          '_internal/config/prices.json']
BASE='https://github.com/tentateurss/TensChep-Workshop/releases/download/'


def trusted(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https' or u.username or u.password or u.port not in (None,443):
        raise ValueError('Untrusted download URL')
    if u.hostname not in ('github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'):
        raise ValueError('Untrusted download host')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req,fp,code,msg,headers,trusted(newurl))


def download(url,path,limit):
    trusted(url)
    opener=urllib.request.build_opener(Redirect())
    with opener.open(urllib.request.Request(url,headers={'User-Agent':'TensChep-Workshop updater'}),timeout=30) as response, open(path,'wb') as out:
        total=0
        while True:
            chunk=response.read(65536)
            if not chunk:break
            total+=len(chunk)
            if total>limit:raise ValueError('Download size limit exceeded')
            out.write(chunk)


def extract(archive,digest,destination):
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest.lower():
        raise ValueError('SHA-256 mismatch')
    with zipfile.ZipFile(archive) as z:
        entries=z.infolist()
        if len(entries)>5000 or sum(x.file_size for x in entries)>600*1024*1024:
            raise ValueError('Archive size limit exceeded')
        seen=set()
        for entry in entries:
            path=PurePosixPath(entry.filename)
            if (not path.parts or path.parts[0]!=FOLDER or path.is_absolute() or
                    '..' in path.parts or (len(path.parts)>1 and path.parts[1] not in (EXE, '_internal', 'README.md', 'PYTHON-LICENSE.txt')) or '\\' in entry.filename or ':' in entry.filename or
                    any(part.endswith((' ','.')) or re.match(r'(?i)^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)',part) for part in path.parts) or
                    stat.S_ISLNK(entry.external_attr>>16) or entry.flag_bits&1):
                raise ValueError('Unsafe ZIP member')
            key=entry.filename.casefold().rstrip('/')
            if key in seen:raise ValueError('Duplicate ZIP member')
            seen.add(key)
        required={FOLDER+'/'+name for name in REQUIRED}
        if not required.issubset({e.filename for e in entries}):
            raise ValueError('Incomplete portable archive')
        z.extractall(destination)
    return destination/FOLDER


def write_job(path,job):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(job),encoding='utf-8');tmp.replace(path)


def swap(target,new,backup,health,launch):
    """All paths belong to the application directory's parent, never user data."""
    if backup.exists():raise ValueError('Previous backup exists; manual recovery required')
    target.rename(backup)
    process=None
    try:
        new.rename(target)
        process=launch(target/EXE)
        if not health(process):raise RuntimeError('New application failed health check')
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate();process.wait(timeout=15)
        if target.exists():target.rename(new)
        backup.rename(target)
        raise


def recover_interrupted(target, backup):
    if backup.exists() and not target.exists():
        backup.rename(target)
        return True
    return False


def apply(job_path):
    job_path=Path(job_path).resolve();job=json.loads(job_path.read_text(encoding='utf-8'))
    target=Path(job['target']);new=Path(job['new']);backup=Path(job['backup'])
    # The helper runs from a separate copy; refuse unexpected directory relationships.
    if not target.is_absolute() or target.parent!=backup.parent or new.parent.parent!=target.parent:
        raise ValueError('Invalid installer paths')
    if os.name!='nt':raise RuntimeError('Windows installer only')
    import ctypes
    kernel=ctypes.windll.kernel32
    kernel.OpenProcess.restype=ctypes.c_void_p
    handle=kernel.OpenProcess(0x100000,False,job['pid'])
    if handle:
        try:
            if kernel.WaitForSingleObject(ctypes.c_void_p(handle),60000)!=0:
                raise RuntimeError('Application is still running')
        finally:kernel.CloseHandle(ctypes.c_void_p(handle))
    nonce=uuid.uuid4().hex
    def launch(exe):
        return subprocess.Popen([str(exe),'serve','--no-browser','--host','127.0.0.1','--port','8765'],env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1',POECRAFT_UPDATE_INSTANCE=nonce),cwd=target)
    def health(process):
        for _ in range(60):
            if process.poll() is not None:return False
            try:
                with urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=1) as response:
                    result=json.load(response)
                    if result.get('version')==job['version'].lstrip('v') and result.get('update_instance')==nonce:return True
            except (OSError,ValueError):pass
            time.sleep(.5)
        return False
    if job.get('state') == 'installing' and recover_interrupted(target, backup):
        job['state']='rolled_back';write_job(job_path,job)
        launch(target/EXE)
        (target.parent/('.'+target.name+'.update.lock')).unlink(missing_ok=True)
        return
    job['state']='installing';write_job(job_path,job)
    try:
        swap(target,new,backup,health,launch)
        job['state']='installed';write_job(job_path,job)
    except BaseException as error:
        job['state']='rolled_back';job['error']=str(error);write_job(job_path,job)
        if (target/EXE).exists():launch(target/EXE)
        raise
    finally:
        (target.parent/('.'+target.name+'.update.lock')).unlink(missing_ok=True)


def prepare(tag,current,progress=lambda state: None):
    from .appupdates import version
    if not getattr(sys,'frozen',False) or os.name!='nt':raise ValueError('Automatic installation requires the Windows portable build')
    if version(tag)<=version(current):raise ValueError('Not a newer version')
    target=Path(sys.executable).resolve().parent
    from . import config
    if config.DATA_DIR.resolve() == target or target in config.DATA_DIR.resolve().parents:
        raise ValueError('User data is inside the application directory; use manual update')
    lock=target.parent/('.'+target.name+'.update.lock')
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
    try:
        if shutil.disk_usage(target.parent).free < 900*1024*1024 + sum(p.stat().st_size for p in target.rglob('*') if p.is_file()):raise OSError('At least 1.2 GB of free space is required')
        work=Path(tempfile.mkdtemp(prefix='.workshop-update-',dir=target.parent))
        base=BASE+tag+'/'
        sums=work/'SHA256SUMS.txt';download(base+'SHA256SUMS.txt',sums,1024*1024)
        matches=re.findall(r'(?m)^([a-fA-F0-9]{64})\s+\*?'+re.escape(ARCHIVE)+r'\s*$',sums.read_text())
        if len(matches)!=1:raise ValueError('Missing or ambiguous archive checksum')
        archive=work/ARCHIVE;download(base+ARCHIVE,archive,200*1024*1024)
        progress("verifying")
        new=extract(archive,matches[0],work)
        progress("preparing")
        helper=work/'helper';shutil.copytree(target,helper)
        job={'pid':os.getpid(),'target':str(target),'new':str(new),'backup':str(target.with_name(target.name+'.previous-'+work.name.rsplit('-',1)[-1])),'version':tag,'state':'prepared'}
        path=work/'installation.json';write_job(path,job)
        subprocess.Popen([str(helper/EXE),'--apply-update',str(path)],env=dict(os.environ,PYINSTALLER_RESET_ENVIRONMENT='1'),cwd=work)
        return path
    except BaseException:
        lock.unlink(missing_ok=True)
        raise
