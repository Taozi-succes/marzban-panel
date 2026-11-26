"""Actual loopback OpenSSH and SQLite, with a fixture Docker adapter.

Run inside Docker with --network none. The source Docker API is simulated;
the separate integration.py exercises actual Docker panel runtimes.
"""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'deploy/operations'),str(ROOT/'tests/operations')]
from common import ToolError,canonical,private_write,write_json
from database import inventory,same_data
from exporter import app_fingerprint
from ssh_transport import SSH
from test_operations import config,fixture_db
import wizard


FAKE_DOCKER=r'''#!/usr/local/bin/python3
import json,os,pathlib,sys
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
sys.path.insert(0,str(pathlib.Path(os.environ['FIXTURE_TOOLS'])))
from bundle import pack
from database import snapshot,inventory
args=sys.argv[1:]
state=json.loads((root/'source-state.json').read_text())
if args[0]=='ps':print('old-panel')
elif args[0]=='inspect':
    if args[1] not in ('old-panel',state['Id']):raise SystemExit(2)
    print(json.dumps([state]))
elif args[0]=='stop':
    if args[1]!=state['Id']:raise SystemExit(2)
    state['State']['Running']=False
    (root/'source-state.json').write_text(json.dumps(state));print(state['Id'])
elif args[0]=='run' and args[-1]=='fingerprint':
    assert args[args.index('--volumes-from')+1]==state['Id']+':ro'
    print(os.environ['FIXTURE_FINGERPRINT'])
elif args[0]=='run':
    assert '--network' in args and args[args.index('--network')+1]=='none'
    assert args[args.index('--volumes-from')+1]==state['Id']+':ro'
    metadata=json.loads(args[-1])
    metadata.update(profile=inventory(root/'source.sqlite3'),app_fingerprint=os.environ['FIXTURE_FINGERPRINT'],local_exits={'users':0,'host_rows':0})
    files={'data/db.sqlite3':snapshot(root/'source.sqlite3'),'data/xray.json':b'{}','source.env.json':b'{}'}
    sys.stdout.buffer.write(pack(files,metadata))
else:raise SystemExit(2)
'''


class Destination:
    def __init__(self,root,fingerprint):self.root=root;self.fingerprint=fingerprint;self.starts=0
    def compose(self,*args,data=None):
        if 'fingerprint' in args:return self.fingerprint.encode()
        if args==('ps','-q','panel'):return b'f'*64
        if args==('stop','panel'):return b''
        raise AssertionError(args)
    def save_toolkit(self):pass
    def render(self,config,stage):
        private_write(self.root/'compose.yaml',json.dumps({'stage':stage}))
        private_write(self.root/'nginx.conf','fixture')
    def start(self):self.starts+=1


def main():
    with tempfile.TemporaryDirectory(prefix='panel-ssh-fixture-',dir='/root') as folder:
        root=Path(folder);target=root/'target';target.mkdir(mode=0o700)
        (target/'data').mkdir();fixture_db(target/'data/db.sqlite3');fixture_db(root/'source.sqlite3')
        import sqlite3
        with sqlite3.connect(target/'data/db.sqlite3') as db:
            for table in ('users','nodes','proxies','allowed_inbounds_association','hosts','node_usages'):db.execute('DELETE FROM '+table)
        cfg=config();write_json(target/'state.json',{'mode':'fresh','phase':'active','config':cfg})
        for name in ('.env','compose.yaml','nginx.conf'):private_write(target/name,'fixture')
        source_id=secrets.token_hex(32)
        source={'Id':source_id,'Name':'/old-panel','Image':'fixture-image','Config':{'Image':'fixture/marzban','Env':[]},'State':{'Running':True}}
        write_json(root/'source-state.json',source)
        before=inventory(root/'source.sqlite3');fingerprint=app_fingerprint()
        (root/'bin').mkdir();private_write(root/'bin/docker',FAKE_DOCKER,0o700)
        for name in ('host','client'):
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/name)],check=True)
        private_write(root/'authorized_keys',(root/'client.pub').read_bytes())
        Path('/run/sshd').mkdir(mode=0o755,exist_ok=True)
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        server_config=f'''Port {port}
ListenAddress 127.0.0.1
HostKey {root}/host
AuthorizedKeysFile {root}/authorized_keys
PidFile {root}/sshd.pid
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes yes
SetEnv PATH={root}/bin:/usr/local/bin:/usr/bin:/bin FIXTURE_ROOT={root} FIXTURE_TOOLS={ROOT}/deploy/operations FIXTURE_FINGERPRINT={fingerprint}
'''
        private_write(root/'sshd.conf',server_config)
        server=subprocess.Popen(['/usr/sbin/sshd','-D','-f',str(root/'sshd.conf'),'-E',str(root/'sshd.log')])
        try:
            for _ in range(50):
                try:
                    with socket.create_connection(('127.0.0.1',port),.2):break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('Fixture SSH did not listen')
            (target/'migration').mkdir(mode=0o700)
            host_public=' '.join((root/'host.pub').read_text().split()[:2])
            private_write(target/'migration/known_hosts',f'[127.0.0.1]:{port} '+host_public+'\n')
            dest=Destination(target,fingerprint)
            def answers(action,confirm=None):
                result=[action,'127.0.0.1',str(port),'root','2',str(root/'client'),'no','8.8.8.8','old.example.test','migrate']
                journal_path=target/'migration/wizard.json'
                if not journal_path.exists():result+=['old-panel']
                if confirm is not None:result+=[confirm]
                return result
            def run_wizard(action,confirm=None):
                with patch('wizard.ask',side_effect=answers(action,confirm)),patch('wizard.getpass.getpass',return_value='fixture-backup-passphrase'):
                    wizard.wizard(dest)
            run_wizard('1')
            assert json.loads((root/'source-state.json').read_text())['State']['Running']
            same_data(before,inventory(root/'source.sqlite3'))
            same_data(before,inventory(target/'data/db.sqlite3'))
            assert inventory(target/'data/db.sqlite3')['node_statuses'][0]['status']=='disabled'
            assert json.loads((target/'state.json').read_text())['phase']=='staging'
            print('PASS: real SSH authentication, verified upload, encrypted transfer and SQLite staging; source stays running',flush=True)
            run_wizard('2','cancel')
            assert json.loads((root/'source-state.json').read_text())['State']['Running']
            print('PASS: cancelling cutover leaves source running',flush=True)
            run_wizard('2','cutover old-panel')
            assert not json.loads((root/'source-state.json').read_text())['State']['Running']
            assert json.loads((target/'state.json').read_text())['phase']=='active'
            journal=json.loads((target/'migration/wizard.json').read_text())
            assert journal['phase']=='complete'
            assert 'fixture-backup-passphrase' not in json.dumps(journal)
            same_data(before,inventory(target/'data/db.sqlite3'))
            assert not list(Path('/tmp').glob('marzban-migrate.*'))
            print('PASS: confirmed cutover, source receipt, final restore, identity/expiry/traffic/grants preserved and tools cleaned',flush=True)
            # Pinning the wrong host key must fail before any source command.
            subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-f',str(root/'wrong-host')],check=True)
            bad_host=' '.join((root/'wrong-host.pub').read_text().split()[:2])
            private_write(target/'migration/known_hosts',f'[127.0.0.1]:{port} '+bad_host+'\n')
            connection=dict(host='127.0.0.1',port=port,user='root',auth='key',key_path=str(root/'client'),sudo=False)
            try:
                with SSH(connection,target/'migration'):raise AssertionError('Wrong SSH host key accepted')
            except ToolError:pass
            print('PASS: SSH host-key mismatch rejected',flush=True)
        finally:
            server.terminate();server.wait(timeout=10)


if __name__=='__main__':main()
