"""Exercise both documented shell entry points without contacting GitHub."""
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class Bootstrap(unittest.TestCase):
    def check(self,entry,mode='ok'):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'bin').mkdir()
            output=io.BytesIO()
            with tarfile.open(fileobj=output,mode='w:gz') as tar:
                for path in (ROOT/'deploy/operations').iterdir():
                    if not path.is_file() or not (path.suffix=='.py' or path.name=='SHA256SUMS'):continue
                    data=path.read_bytes()
                    if mode=='tampered' and path.name=='panel.py':data+=b'# tampered\n'
                    item=tarfile.TarInfo('repository/deploy/operations/'+path.name);item.size=len(data)
                    tar.addfile(item,io.BytesIO(data))
            (root/'archive').write_bytes(output.getvalue())
            curl=root/'bin/curl'
            curl.write_text('''#!/usr/bin/env python3
import json,os,pathlib,shutil,sys
pathlib.Path(os.environ['CALLS']).write_text(json.dumps(sys.argv))
if os.environ['MODE']=='download-error':sys.exit(22)
shutil.copyfile(os.environ['ARCHIVE'],sys.argv[sys.argv.index('--output')+1])
''');curl.chmod(0o700)
            env=dict(os.environ,PATH=str(root/'bin')+':'+os.environ['PATH'],CALLS=str(root/'calls'),
                     MODE=mode,ARCHIVE=str(root/'archive'),TMPDIR=str(root),GITHUB_TOKEN='unused-fixture-token')
            result=subprocess.run(['bash',str(ROOT/entry),'--help'],capture_output=True,env=env,timeout=20)
            if mode=='ok':self.assertEqual(result.returncode,0,result.stderr.decode())
            else:self.assertNotEqual(result.returncode,0)
            calls=(root/'calls').read_text()
            self.assertNotIn('Authorization',calls);self.assertNotIn('unused-fixture-token',calls)
            self.assertNotIn(b'unused-fixture-token',result.stdout+result.stderr)
            self.assertEqual({p.name for p in root.iterdir()},{'bin','calls','archive'})
    def test_fresh_help_and_cleanup(self):self.check('install.sh')
    def test_migration_help_and_cleanup(self):self.check('migrate.sh')
    def test_tamper_rejected(self):self.check('install.sh','tampered')
    def test_download_failure_stops(self):self.check('migrate.sh','download-error')


if __name__=='__main__':unittest.main()
