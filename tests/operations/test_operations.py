"""Offline protection tests; all databases and secrets below are fixtures."""
import copy
import io
import json
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock,patch

TOOLS=Path(__file__).resolve().parents[2]/'deploy/operations'
sys.path.insert(0,str(TOOLS))
from common import ToolError,canonical,private_write,render_env,parse_env,root_path,validate_config
from bundle import pack,unpack,seal,unseal
from database import inventory,snapshot,same_data,disable_nodes,restore_node_statuses
from host import compose_config,nginx_config
import migrate
import panel


def config():
    return dict(panel_domain='panel.example.com',sub_domain='sub.example.com',zone='example.com',
                email='admin@example.com',address='8.8.8.8',image='ghcr.io/example/panel:v1.0.0',http_port=18080)


def fixture_db(path):
    with sqlite3.connect(path) as db:
        db.executescript('''
        CREATE TABLE users(id INTEGER, username TEXT, expire INTEGER, used_traffic INTEGER);
        INSERT INTO users VALUES(1,'fixture',2100000000,1234567890123);
        CREATE TABLE admins(id INTEGER, hashed_password TEXT);
        INSERT INTO admins VALUES(1,'fixture-hash');
        CREATE TABLE proxies(id INTEGER, user_id INTEGER, settings TEXT);
        INSERT INTO proxies VALUES(1,1,'{"id":"fixture-uuid"}');
        CREATE TABLE hosts(id INTEGER, inbound_tag TEXT, address TEXT, is_disabled INTEGER);
        CREATE TABLE nodes(id INTEGER, status TEXT, name TEXT);
        INSERT INTO nodes VALUES(1,'connected','fixture');
        CREATE TABLE jwt(key TEXT); INSERT INTO jwt VALUES('fixture-jwt-secret');
        CREATE TABLE tls(key TEXT); INSERT INTO tls VALUES('fixture-tls-private');
        CREATE TABLE allowed_inbounds_association(proxy_id INTEGER, inbound_tag TEXT);
        INSERT INTO allowed_inbounds_association VALUES(1,'manual-grant');
        CREATE TABLE alembic_version(version_num TEXT); INSERT INTO alembic_version VALUES('1fd4ac8350e9');
        CREATE TABLE node_usages(uplink INTEGER); INSERT INTO node_usages VALUES(9876543210123);
        ''')


class FilesAndConfig(unittest.TestCase):
    def test_literal_environment_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'env'
            values={'A':'spaces $x "quotes" #value = ok','B':"'literal'",'EMPTY':''}
            private_write(path,render_env(values))
            self.assertEqual(values,parse_env(path))
            self.assertEqual(path.stat().st_mode&0o777,0o600)

    def test_newline_env_rejected(self):
        for value in ('a\nb','a\rb','a\0b'):
            with self.assertRaises(ToolError):render_env({'A':value})

    def test_no_symlink_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'link').symlink_to(root/'target')
            with self.assertRaises(ToolError):private_write(root/'link',b'x')
            self.assertFalse((root/'target').exists())

    def test_root_and_domains(self):
        for value in ('/','/opt','/opt/../etc','/tmp/panel','/opt/panel/child'):
            with self.assertRaises(ToolError):root_path(value)
        self.assertEqual(str(root_path('/opt/test-panel')),'/opt/test-panel')
        validate_config(config())
        for key,value in [('sub_domain','panel.example.com'),('panel_domain','evil.test'),('image','ghcr.io/x/y:latest')]:
            invalid=dict(config(),**{key:value})
            with self.assertRaises(ToolError):validate_config(invalid)

    def test_isolated_network_and_raw_env(self):
        value=compose_config(config(),'/opt/fixture',True)
        self.assertEqual(value['services']['panel']['networks'],['backend'])
        self.assertTrue(value['networks']['backend']['internal'])
        self.assertEqual(value['services']['panel']['environment']['TELEGRAM_API_TOKEN'],'')
        self.assertEqual(value['services']['panel']['ports'],['127.0.0.1:18080:8000'])
        self.assertEqual(value['services']['panel']['env_file'][0]['format'],'raw')
        live=compose_config(config(),'/opt/fixture',False)
        self.assertIn('controller',live['services']['panel']['networks'])
        self.assertEqual(live['services']['ingress']['ports'],['443:443'])

    def test_nginx_subscription_and_origin_limits(self):
        text=nginx_config(dict(config(),cloudflare_ranges=['173.245.48.0/20']))
        sub=text.split('server_name sub.example.com;')[1]
        self.assertIn('location / { return 404; }',sub)
        self.assertIn('location ^~ /sub/',sub)
        self.assertEqual(text.count('deny all;'),2)
        self.assertEqual(text.count('allow 173.245.48.0/20;'),2)

    def test_empty_core_has_no_assignable_protocols(self):
        core=panel.empty_core()
        self.assertTrue(all(i['listen']=='127.0.0.1' and i['protocol']=='socks' for i in core['inbounds']))


class DatabaseIntegrity(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'db.sqlite3';fixture_db(self.path)
    def tearDown(self):self.temp.cleanup()

    def test_online_snapshot_and_status_only_change(self):
        before=inventory(self.path)
        self.assertEqual(before,inventory(raw=snapshot(self.path)))
        disable_nodes(self.path);same_data(before,inventory(self.path))
        self.assertEqual(inventory(self.path)['node_statuses'],[{'id':1,'status':'disabled'}])
        restore_node_statuses(self.path,before['node_statuses'])
        self.assertEqual(before,inventory(self.path))

    def test_all_business_fields_checked(self):
        for table,column,value in [('users','expire',42),('users','used_traffic',9),('proxies','settings','changed'),
                                   ('allowed_inbounds_association','inbound_tag','changed'),('jwt','key','changed'),
                                   ('tls','key','changed'),('node_usages','uplink',0)]:
            before=inventory(self.path)
            with sqlite3.connect(self.path) as db:db.execute(f'UPDATE {table} SET {column}=?',(value,))
            with self.assertRaises(ToolError):same_data(before,inventory(self.path))

    def test_schema_rejected(self):
        with sqlite3.connect(self.path) as db:db.execute("UPDATE alembic_version SET version_num='stock'")
        with self.assertRaises(ToolError):inventory(self.path)


class EncryptedBundles(unittest.TestCase):
    def files(self):return {'data/db.sqlite3':b'fixture','data/xray.json':b'{}','source.env.json':b'{}'}
    def test_encryption_roundtrip_and_wrong_password(self):
        payload=pack(self.files(),{'kind':'preview'})
        encrypted=seal(payload,'fixture-passphrase')
        self.assertNotIn(b'fixture',encrypted)
        self.assertEqual(unseal(encrypted,'fixture-passphrase'),payload)
        for password,data in [('wrong-password',encrypted),('fixture-passphrase',encrypted[:-1]+bytes([encrypted[-1]^1]))]:
            with self.assertRaises(ToolError):unseal(data,password)
        with tempfile.TemporaryDirectory() as folder:
            manifest=unpack(payload,folder)
            self.assertEqual(manifest['kind'],'preview')
            self.assertEqual((Path(folder)/'data/db.sqlite3').read_bytes(),b'fixture')

    def test_traversal_and_alias_rejected_before_writing(self):
        for bad in ('data/../../escape','/data/absolute','data/./db.sqlite3','data//db.sqlite3','data/back\\slash'):
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ToolError):unpack(pack(dict(self.files(),**{bad:b'bad'}),{}),folder)
                self.assertEqual(list(Path(folder).iterdir()),[])

    def test_duplicate_and_links(self):
        for symlink in (True,False):
            output=io.BytesIO()
            with tarfile.open(fileobj=output,mode='w:gz') as tar:
                item=tarfile.TarInfo('data/db.sqlite3')
                if symlink:item.type=tarfile.SYMTYPE;item.linkname='/tmp/elsewhere';tar.addfile(item)
                else:tar.addfile(item);tar.addfile(item)
            with tempfile.TemporaryDirectory() as folder:
                with self.assertRaises(ToolError):unpack(output.getvalue(),folder)


class CutoverProtections(unittest.TestCase):
    def test_final_export_requires_stopped_source(self):
        with patch.object(migrate,'inspect_container',return_value={'Config':{'Image':'fixture'},'State':{'Running':True}}),patch.object(migrate,'run') as run:
            with self.assertRaises(ToolError):migrate.export_container('fixture','final',[])
            run.assert_not_called()

    def test_preview_cannot_activate(self):
        with tempfile.TemporaryDirectory() as folder:
            host=Mock(root=Path(folder))
            state={'mode':'migration','phase':'staging','manifest':{'kind':'preview','source_stopped':False}}
            (host.root/'state.json').write_text(json.dumps(state))
            with self.assertRaises(ToolError):migrate.activate(host)
            host.compose.assert_not_called()

    def test_restore_refuses_existing_business_before_reading_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            host=Mock(root=Path(folder));(host.root/'data').mkdir()
            fixture_db(host.root/'data/db.sqlite3')
            (host.root/'state.json').write_text(json.dumps({'mode':'fresh','phase':'active'}))
            with self.assertRaises(ToolError):migrate.restore(host,'missing-bundle','unused')
            host.compose.assert_not_called()

    def test_fresh_refuses_unknown_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            host=Mock(root=Path(folder));(host.root/'foreign-file').touch()
            with self.assertRaises(ToolError):panel.install(host)
            host.dependencies.assert_not_called()

    def test_recovery_restores_destination_without_touching_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);saved=root/'releases/restore-fixture';saved.mkdir(parents=True)
            (saved/'data').mkdir();(saved/'data/original').write_text('old')
            (root/'data').mkdir();(root/'data/imported').write_text('new')
            original={'mode':'fresh','phase':'active','config':config()}
            (saved/'state.json').write_text(json.dumps(original))
            (root/'state.json').write_text(json.dumps(dict(original,phase='restoring',recovery='releases/restore-fixture')))
            host=Mock(root=root)
            migrate.recover(host)
            self.assertTrue((root/'data/original').exists())
            self.assertTrue((saved/'failed-data/imported').exists())
            self.assertEqual(json.loads((root/'state.json').read_text()),original)
            host.compose.assert_called_once_with('stop','panel')


if __name__=='__main__':unittest.main()
