"""Real panel runtime test with disposable Docker volumes and no cloud access."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]


def execute(args,data=None,check=True):
    result=subprocess.run(args,input=data,capture_output=True,timeout=120)
    if check and result.returncode:
        raise RuntimeError('Test command failed: '+result.stderr.decode(errors='replace')[-3000:])
    return result.stdout


def main(image):
    prefix='panel-test-'+uuid.uuid4().hex[:12]
    containers=[];volumes=[];network=prefix+'-isolated'
    execute(['docker','network','create','--internal',network])
    tools=str(ROOT/'deploy/operations')+':/operator:ro'
    fixture=str(ROOT/'tests/operations')+':/checks:ro'
    env=['-e','SQLALCHEMY_DATABASE_URL=sqlite:////var/lib/marzban/db.sqlite3','-e','XRAY_JSON=/var/lib/marzban/xray.json',
         '-e','XRAY_EXECUTABLE_PATH=/usr/local/bin/xray','-e','TELEGRAM_API_TOKEN=','-e','WEBHOOK_ADDRESS=',
         '-e','DISCORD_WEBHOOK_URL=','-e','PYTHONPATH=/operator:/code']
    def volume(suffix):
        name=prefix+'-'+suffix;execute(['docker','volume','create',name]);volumes.append(name);return name
    def helper(vol,*args,data=None,readonly=False):
        return execute(['docker','run','--rm','-i','--network','none',*env,'-v',tools,'-v',fixture,
                        '-v',vol+':/var/lib/marzban'+(':ro' if readonly else ''),'--entrypoint','python',image,
                        '/checks/fixture.py',*args],data)
    def start(vol,suffix,stage):
        name=prefix+'-'+suffix;containers.append(name)
        execute(['docker','run','-d','--name',name,'--network',network,*env,'-e','PANEL_STAGE='+stage,
                 '-v',tools,'-v',vol+':/var/lib/marzban','--entrypoint','python',image,'/operator/runtime.py'])
        for _ in range(40):
            check=subprocess.run(['docker','exec',name,'python','-c',"from urllib.request import urlopen;urlopen('http://127.0.0.1:8000/_panel_health',timeout=1)"],capture_output=True)
            if check.returncode==0:return name
            time.sleep(1)
        raise RuntimeError('Runtime did not become healthy: '+execute(['docker','logs',name],check=False).decode(errors='replace')[-1500:])
    def inside(name,code):return execute(['docker','exec',name,'python','-c',code]).decode().strip()
    login="""
import json
from urllib.request import Request,urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
base='http://127.0.0.1:8000'
body=urlencode({'username':'fixture_admin','password':'fixture-password-123'}).encode()
token=json.load(urlopen(Request(base+'/api/admin/token',data=body)))['access_token']
headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
"""
    try:
        fresh=volume('fresh');print(helper(fresh,'prepare','fresh').decode().strip(),flush=True)
        fresh_app=start(fresh,'fresh-app','live')
        print(inside(fresh_app,login+"""
assert json.load(urlopen(Request(base+'/api/users',headers=headers)))['total']==0
assert json.load(urlopen(Request(base+'/api/nodes',headers=headers)))==[]
new=json.load(urlopen(Request(base+'/api/user',headers=headers,data=json.dumps({'username':'fixture_new','proxies':{},'inbounds':{}}).encode())))
assert new['inbounds']=={} and new['links']==[]
print('Fresh panel: login works, zero old users/nodes, new user has no grants')
"""),flush=True)
        source=volume('source');print(helper(source,'prepare','source').decode().strip(),flush=True)
        source_container=prefix+'-source';containers.append(source_container)
        execute(['docker','run','-d','--name',source_container,'--network','none','-v',source+':/var/lib/marzban','--entrypoint','sleep',image,'infinity'])
        preview=helper(source,'export','preview',readonly=True)
        target=volume('target');print(helper(target,'restore',data=preview).decode().strip(),flush=True)
        stage=start(target,'stage-app','staging')
        print(inside(stage,login+"""
import socket
users=json.load(urlopen(Request(base+'/api/users',headers=headers)))['users']
old=next(u for u in users if u['username']=='fixture_granted')
assert old['expire']==1500000000 and old['used_traffic']==1234567890123
assert old['inbounds']=={'vless':['fixture-vless']}
for path,method,expected in [('/api/user','POST',409),('/sub/fixture','GET',503)]:
    try:urlopen(Request(base+path,headers=headers,data=b'{}' if method=='POST' else None,method=method))
    except HTTPError as error:assert error.code==expected
    else:raise AssertionError('Staging unexpectedly allowed mutation or subscription')
try:socket.create_connection(('1.1.1.1',443),1)
except OSError:pass
else:raise AssertionError('Isolated stage has internet egress')
print('Staging: original admin and grants work; writes/subscriptions blocked; internet egress blocked')
"""),flush=True)
        time.sleep(12)
        processes=execute(['docker','top',stage,'-eo','pid,comm']).decode().splitlines()
        assert not any(line.split()[-1].startswith('xray') for line in processes if line.split()),processes
        print(helper(target,'verify',readonly=True).decode().strip(),flush=True)
        execute(['docker','stop',stage])
        execute(['docker','stop',source_container])
        assert not json.loads(execute(['docker','inspect',source_container]))[0]['State']['Running']
        final=helper(source,'export','final',readonly=True)
        final_target=volume('final');helper(final_target,'restore',data=final);helper(final_target,'activate')
        active=start(final_target,'active-app','live')
        print(inside(active,login+"""
users=json.load(urlopen(Request(base+'/api/users',headers=headers)))['users']
old=next(u for u in users if u['username']=='fixture_granted')
assert old['used_traffic']==1234567890123 and old['expire']==1500000000
assert old['inbounds']=={'vless':['fixture-vless']}
print('Final cutover: preserved credentials, expiry, traffic and grants; source container remains stopped')
"""),flush=True)
        assert not json.loads(execute(['docker','inspect',source_container]))[0]['State']['Running']
        print('PASS: isolated fresh installation and migration runtime',flush=True)
    finally:
        for name in reversed(containers):execute(['docker','rm','-f',name],check=False)
        for name in reversed(volumes):execute(['docker','volume','rm',name],check=False)
        execute(['docker','network','rm',network],check=False)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--image',required=True)
    main(parser.parse_args().image)
