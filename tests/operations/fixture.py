"""Disposable integration data. Executed only in a test Docker volume."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path[:0]=['/operator','/code']
from common import canonical
from database import disable_nodes,inventory,restore_node_statuses,same_data
from bundle import unpack,seal,unseal

ROOT=Path('/var/lib/marzban')
mode=sys.argv[1]
if mode=='prepare':
    from panel import empty_core
    ROOT.mkdir(exist_ok=True)
    core=empty_core()
    if sys.argv[2]=='source':
        core['inbounds'].append({'tag':'fixture-vless','listen':'127.0.0.1','port':19002,'protocol':'vless',
                                 'settings':{'decryption':'none','clients':[]}})
    (ROOT/'xray.json').write_bytes(canonical(core))
    subprocess.run(['alembic','upgrade','head'],check=True,capture_output=True)
    payload=canonical({'username':'fixture_admin','password':'fixture-password-123'})
    subprocess.run(['python','/operator/initialize.py'],input=payload,check=True,capture_output=True)
    from app import xray
    from app.db import GetDB,crud
    from app.db.models import Node
    from app.models.user import UserCreate
    with GetDB() as db:
        crud.get_jwt_secret_key(db);crud.get_tls_certificate(db);crud.get_system_usage(db)
        for tag in xray.config.inbounds_by_tag:crud.get_hosts(db,tag)
        if sys.argv[2]=='source':
            crud.create_user(db,UserCreate(username='fixture_granted',proxies={'vless':{}},inbounds={'vless':['fixture-vless']},
                                          status='active',expire=1500000000,data_limit=5000000000000))
            crud.create_user(db,UserCreate(username='fixture_blank',proxies={'vless':{}},inbounds={},status='active'))
            db.add(Node(name='fixture-node',address='127.0.0.1',port=62050,api_port=62051,status='connected',uplink=123,downlink=456))
            db.commit()
    if sys.argv[2]=='source':
        with sqlite3.connect(ROOT/'db.sqlite3') as db:
            db.execute("UPDATE users SET used_traffic=1234567890123 WHERE username='fixture_granted'")
            db.execute("UPDATE users SET status='disabled' WHERE username='fixture_blank'")
    print('Fixture database prepared')
elif mode=='export':
    from exporter import collect
    preview=collect({'kind':sys.argv[2],'source_stopped':sys.argv[2]=='final','source_container':'fixture-source',
                     'source_addresses':[]})
    # Exercise authenticated encryption with a fixture password, never real data.
    assert unseal(seal(preview,'fixture-backup-password'),'fixture-backup-password')==preview
    sys.stdout.buffer.write(preview)
elif mode=='restore':
    manifest=unpack(sys.stdin.buffer.read(),ROOT/'incoming')
    source=ROOT/'incoming/data'
    for path in source.iterdir():path.rename(ROOT/path.name)
    same_data(manifest['profile'],inventory(ROOT/'db.sqlite3'))
    disable_nodes(ROOT/'db.sqlite3')
    (ROOT/'manifest.json').write_bytes(canonical(manifest))
    same_data(manifest['profile'],inventory(ROOT/'db.sqlite3'))
    print('All tables, identity, traffic, expiry and grants preserved')
elif mode=='verify':
    manifest=json.loads((ROOT/'manifest.json').read_bytes())
    same_data(manifest['profile'],inventory(ROOT/'db.sqlite3'))
    assert inventory(ROOT/'db.sqlite3')['node_statuses']==[{'id':1,'status':'disabled'}]
    print('Isolated database unchanged')
elif mode=='activate':
    manifest=json.loads((ROOT/'manifest.json').read_bytes())
    assert manifest['kind']=='final' and manifest['source_stopped']
    restore_node_statuses(ROOT/'db.sqlite3',manifest['profile']['node_statuses'])
    same_data(manifest['profile'],inventory(ROOT/'db.sqlite3'))
    print('Final snapshot prepared for activation')
