"""Reject development files and user data in the portable release."""
from pathlib import Path, PurePosixPath
import sys
import zipfile

def check(archive):
    forbidden={'tests','__pycache__','.git','.venv','venv','node_modules','raw',
               'ninja_cache','trade_cache','build','dist','.browser-tools'}
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:
            raise ValueError('Corrupt ZIP')
        for name in z.namelist():
            p=PurePosixPath(name)
            if (forbidden.intersection(p.parts) or p.name.startswith('.env') or
                    p.suffix.lower() in {'.db','.sqlite','.sqlite3','.log','.tmp','.pem','.key'} or
                    p.name.startswith(('id_rsa','id_ed25519')) or 'backup' in p.name.lower()):
                raise ValueError('Unexpected release file: '+name)
        print('Portable archive audit:',len(z.namelist()),'entries; no development or user data')

if __name__=='__main__':
    check(Path(sys.argv[1]) if len(sys.argv)>1 else Path('dist/TensChep-Workshop-Windows-x64.zip'))
