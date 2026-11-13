import hashlib,tempfile,unittest,zipfile
from pathlib import Path
from unittest.mock import Mock
from poecraft.installer import extract,swap,trusted,FOLDER,EXE,REQUIRED

class PortableInstaller(unittest.TestCase):
 def archive(self,root,names):
  p=root/'update.zip'
  with zipfile.ZipFile(p,'w') as z:
   for n in names:z.writestr(n,b'test')
  return p,hashlib.sha256(p.read_bytes()).hexdigest()
 def test_valid_archive(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p,h=self.archive(root,[FOLDER+'/'+name for name in REQUIRED])
   self.assertTrue((extract(p,h,root/'stage')/EXE).exists())
 def test_hash_and_structure(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);p,h=self.archive(root,['unexpected'])
   with self.assertRaisesRegex(ValueError,'SHA'):extract(p,'0'*64,root/'stage')
   with self.assertRaises(ValueError):extract(p,h,root/'stage')
 def test_traversal_and_windows_paths(self):
  for name in ['../escape',FOLDER+'/../escape',FOLDER+'/C:evil',FOLDER+'/CON.txt',FOLDER+'/bad\\evil']:
   with self.subTest(name=name),tempfile.TemporaryDirectory() as d:
    root=Path(d);p,h=self.archive(root,[name])
    with self.assertRaises(ValueError):extract(p,h,root/'stage')
 def test_untrusted_urls(self):
  for u in ['http://github.com/file','https://evil.invalid/file','https://github.com@evil.invalid/file']:
   with self.assertRaises(ValueError):trusted(u)
 def test_success_preserves_backup_and_user_files(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);old=r/'app';new=r/'stage';backup=r/'backup';old.mkdir();new.mkdir()
   (old/EXE).write_text('old');(new/EXE).write_text('new');(r/'userdata').write_text('keep')
   swap(old,new,backup,lambda p:True,lambda exe:Mock())
   self.assertEqual((old/EXE).read_text(),'new');self.assertEqual((backup/EXE).read_text(),'old');self.assertEqual((r/'userdata').read_text(),'keep')
 def test_failed_launch_restores_old(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);old=r/'app';new=r/'stage';backup=r/'backup';old.mkdir();new.mkdir()
   (old/EXE).write_text('old');(new/EXE).write_text('new')
   with self.assertRaises(RuntimeError):swap(old,new,backup,lambda p:False,lambda exe:Mock())
   self.assertEqual((old/EXE).read_text(),'old')

 def test_symlink_is_rejected(self):
  import stat
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);p=r/'update.zip'
   with zipfile.ZipFile(p,'w') as z:
    entry=zipfile.ZipInfo(FOLDER+'/link');entry.create_system=3;entry.external_attr=(stat.S_IFLNK|0o777)<<16;z.writestr(entry,'../../outside')
   with self.assertRaises(ValueError):extract(p,hashlib.sha256(p.read_bytes()).hexdigest(),r/'stage')
 def test_interrupted_rename_can_restore_backup(self):
  from poecraft.installer import recover_interrupted
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);backup=r/'backup';backup.mkdir();(backup/EXE).write_text('old')
   self.assertTrue(recover_interrupted(r/'app',backup))
   self.assertEqual((r/'app'/EXE).read_text(),'old')
   self.assertFalse(recover_interrupted(r/'app',backup))
 def test_space_failure_and_parallel_lock(self):
  from unittest.mock import patch
  from types import SimpleNamespace
  from poecraft import installer
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);target=r/'app';target.mkdir();exe=target/EXE;exe.write_text('old')
   with patch.object(installer.sys,'frozen',True,create=True),patch.object(installer.sys,'executable',str(exe)),patch.object(installer.shutil,'disk_usage',return_value=SimpleNamespace(free=0)):
    with self.assertRaises(OSError):installer.prepare('v1.0.1','1.0.0')
    lock=r/'.app.update.lock';self.assertFalse(lock.exists())
    lock.write_text('busy')
    with self.assertRaises(FileExistsError):installer.prepare('v1.0.1','1.0.0')
    self.assertTrue(lock.exists())

 def test_readonly_lock_failure_is_reported(self):
  from unittest.mock import patch
  from poecraft import installer
  with patch.object(installer.sys,'frozen',True,create=True),patch.object(installer.os,'open',side_effect=PermissionError('readonly')):
   with self.assertRaises(PermissionError):installer.prepare('v1.0.1','1.0.0')
 def test_duplicate_members_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   r=Path(d);p,h=self.archive(r,[FOLDER+'/'+EXE,FOLDER+'/'+EXE])
   with self.assertRaisesRegex(ValueError,'Duplicate'):extract(p,h,r/'stage')
 def test_corrupt_archive_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'bad.zip';p.write_bytes(b'not a zip')
   with self.assertRaises(zipfile.BadZipFile):extract(p,hashlib.sha256(p.read_bytes()).hexdigest(),Path(d)/'stage')
