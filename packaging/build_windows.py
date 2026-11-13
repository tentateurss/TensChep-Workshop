from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
root = Path(__file__).resolve().parents[1]
subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--name', 'TensChep-Workshop', '--collect-submodules', 'poecraft', '--add-data', 'poecraft/web:poecraft/web', '--add-data', 'poecraft/locales:poecraft/locales', '--add-data', 'poecraft/rules:poecraft/rules', '--add-data', 'config/prices.json:config', 'launcher.py'], cwd=root, check=True)
folder = root / 'dist' / 'TensChep-Workshop'
shutil.copyfile(root / 'docs' / 'RELEASE.md', folder / 'README.md')
# Include the Python runtime license shipped by the interpreter.
license_file = Path(sys.base_prefix) / 'LICENSE.txt'
if license_file.exists():
    shutil.copyfile(license_file, folder / 'PYTHON-LICENSE.txt')
archive = Path(shutil.make_archive(str(root/'dist'/'TensChep-Workshop-Windows-x64'), 'zip', root/'dist', folder.name))
(archive.parent/'SHA256SUMS.txt').write_text(hashlib.sha256(archive.read_bytes()).hexdigest()+'  '+archive.name+'\n')
print(archive, archive.stat().st_size)
