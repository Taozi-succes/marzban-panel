"""Migration orchestration safety tests; no production network or Docker calls."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/operations'))
from bundle import pack
from common import ToolError,canonical,write_json
from database import inventory
import remote as source_tool
from ssh_transport import SSH,connection_config
import wizard
from test_operations import config,fixture_db

SOURCE='a'*64
MIGRATION='b'*32
FINGERPRINT='c'*64
PASSWORD='fixture-password-123'


class WizardSafety(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/'data').mkdir();fixture_db(self.root/'data/db.sqlite3')
        self.profile=inventory(self.root/'data/db.sqlite3')
        self.host=Mock(root=self.root)
        self.journal={'migration_id':MIGRATION,'phase':'preview_restored','source':{'id':SOURCE,'name':'old-panel'},
                      'identity':self.profile['identity'],'connection':{'host':'old.example.test','address':'8.8.8.8','domain':'old.example.test'}}
        write_json(self.root/'state.json',{'mode':'migration','phase':'staging','config':config(),'manifest':{'profile':self.profile}})
        self.remote=Mock();self.calls=[]
        self.record={'source_container':SOURCE,'migration_id':MIGRATION,'receipt_id':'d'*32,'phase':'stopped'}
        def request(op,**values):
            self.calls.append(op)
            if op=='inspect':return {'id':SOURCE,'name':'old-panel','running':True}
            if op=='export':return self.payload()
            if op in ('freeze','stopped'):return self.record
            if op=='final':return self.payload('final',self.record)
            raise AssertionError(op)
        self.remote.request.side_effect=request
    def tearDown(self):self.temp.cleanup()

    def payload(self,kind='preview',record=None,local_users=0):
        return pack({'data/db.sqlite3':(self.root/'data/db.sqlite3').read_bytes(),'data/xray.json':b'{}','source.env.json':b'{}'},
                    {'kind':kind,'source_container':SOURCE,'source_stopped':kind=='final','stop_receipt':record,
                     'profile':self.profile,'app_fingerprint':FINGERPRINT,'local_exits':{'users':local_users,'host_rows':local_users}})

    def invoke(self,answer):
        with patch('wizard.ask',return_value=answer),patch('wizard.migrate.verify'),patch('wizard.migrate.restore',return_value=True) as restore,patch('wizard.migrate.activate',return_value=True) as activate:
            wizard.cutover(self.host,self.remote,self.journal,PASSWORD,Mock(),self.root,FINGERPRINT)
            return restore,activate

    def test_cancel_never_stops_source_or_restores(self):
        restore,activate=self.invoke('no')
        self.assertEqual(self.calls,['inspect','export']);restore.assert_not_called();activate.assert_not_called()

    def test_online_preview_does_not_request_stop(self):
        self.journal['phase']='planned'
        with patch('wizard.migrate.restore',return_value=True),patch('wizard.ask',side_effect=AssertionError('No cutover prompt during preview')):
            wizard.preview(self.host,self.remote,self.journal,PASSWORD,Mock(),self.root,FINGERPRINT)
        self.assertEqual(self.calls,['export']);self.assertEqual(self.journal['phase'],'preview_restored')
        self.assertNotIn(PASSWORD,json.dumps(self.journal))

    def test_final_flow_requires_live_stop_receipt_before_activation(self):
        restore,activate=self.invoke('cutover old-panel')
        self.assertEqual(self.calls,['inspect','export','freeze','final','stopped'])
        restore.assert_called_once();activate.assert_called_once_with(self.host,confirmed=True)
        self.assertEqual(self.journal['phase'],'complete')

    def test_local_exit_grant_blocks_before_confirmation(self):
        original=self.remote.request.side_effect
        self.remote.request.side_effect=lambda op,**v:self.payload(local_users=1) if op=='export' else original(op,**v)
        with self.assertRaises(ToolError):self.invoke('cutover old-panel')
        self.assertNotIn('freeze',self.calls)

    def test_wrong_fingerprint_fails_before_stop(self):
        with self.assertRaises(ToolError):wizard.validate_payload(self.payload(),self.journal['source'],'f'*64)
        self.assertEqual(self.calls,[])

    def test_other_receipt_or_preview_cannot_become_final(self):
        for payload in (self.payload(),self.payload('final',dict(self.record,migration_id='e'*32))):
            with self.assertRaises(ToolError):wizard.validate_payload(payload,self.journal['source'],FINGERPRINT,self.journal['identity'],'final',self.record)

    def test_final_transfer_failure_never_activates_or_restarts_source(self):
        original=self.remote.request.side_effect
        def fail(op,**v):
            if op=='final':raise ToolError('fixture transfer interrupted')
            return original(op,**v)
        self.remote.request.side_effect=fail
        with patch('wizard.migrate.activate') as activate,self.assertRaises(ToolError):self.invoke('cutover old-panel')
        activate.assert_not_called();self.assertEqual(self.journal['phase'],'source_stopped')
        self.assertNotIn('start',self.calls)

    def test_resume_activation_does_not_reimport_or_stop_again(self):
        self.journal.update(phase='activating',receipt=self.record)
        saved=wizard.save_payload(self.root,self.payload('final',self.record),PASSWORD,'final')
        self.journal['final']=saved
        write_json(self.root/'state.json',{'mode':'migration','phase':'activating','config':config()})
        restore,activate=self.invoke('cutover old-panel')
        restore.assert_not_called();activate.assert_called_once()
        self.assertEqual(self.calls,['inspect','stopped'])

    def test_existing_business_refused_before_ssh(self):
        write_json(self.root/'state.json',{'mode':'fresh','phase':'active','config':config()})
        with patch('wizard.SSH') as ssh,self.assertRaises(ToolError):wizard.wizard(self.host)
        ssh.assert_not_called()

    def test_no_args_selects_wizard_not_freeze(self):
        import migrate
        with patch('migrate.os.geteuid',return_value=0),patch('migrate.operator_lock'),patch('migrate.Host'),patch('wizard.wizard') as entry,patch('migrate.backup') as backup:
            migrate.main([])
        entry.assert_called_once();backup.assert_not_called()


class SourceStopReceipt(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source={'Id':SOURCE,'Name':'/old-panel','Image':'fixture-image','Config':{'Image':'fixture'},'State':{'Running':True}}
        self.values={'container':'old-panel','expected_id':SOURCE,'migration_id':MIGRATION,'addresses':[]}
        self.paths=patch('remote.receipt_paths',return_value=(self.root/'lock',self.root/'receipt.json'));self.paths.start()
    def tearDown(self):self.paths.stop();self.temp.cleanup()

    def test_stop_and_retry_are_idempotent_for_same_migration(self):
        after=copy.deepcopy(self.source);after['State']['Running']=False
        with patch('remote.inspect_container',side_effect=[self.source,after,after,after]),patch('remote.run') as run:
            first=source_tool.operate('freeze',self.values)
            second=source_tool.operate('freeze',self.values)
        self.assertEqual(first,second);run.assert_called_once_with(['docker','stop',SOURCE])

    def test_other_destination_cannot_claim_existing_stop(self):
        write_json(self.root/'receipt.json',{'migration_id':'e'*32,'source_container':SOURCE,'phase':'stopped'})
        with patch('remote.inspect_container',return_value=self.source),patch('remote.run') as run,self.assertRaises(ToolError):
            source_tool.operate('freeze',self.values)
        run.assert_not_called()

    def test_source_restarted_blocks_final_and_activation_check(self):
        write_json(self.root/'receipt.json',{'migration_id':MIGRATION,'source_container':SOURCE,'phase':'stopped'})
        for operation in ('freeze','final','stopped'):
            with patch('remote.inspect_container',return_value=self.source),patch('remote.run') as run,self.assertRaises(ToolError):
                source_tool.operate(operation,self.values)
            run.assert_not_called()

    def test_replaced_container_is_rejected(self):
        other=copy.deepcopy(self.source);other['Id']='f'*64
        with patch('remote.inspect_container',return_value=other),patch('remote.run') as run,self.assertRaises(ToolError):source_tool.operate('freeze',self.values)
        run.assert_not_called()

    def test_stopped_without_our_receipt_is_not_claimed(self):
        self.source['State']['Running']=False
        with patch('remote.inspect_container',return_value=self.source),patch('remote.run') as run,self.assertRaises(ToolError):source_tool.operate('freeze',self.values)
        run.assert_not_called()


class SSHConfiguration(unittest.TestCase):
    def test_injection_hosts_and_users_rejected(self):
        base={'host':'old.example.test','port':22,'user':'root','auth':'password','sudo':False}
        for field,value in [('host','-oProxyCommand=evil'),('host','example.test;touch /tmp/no'),('host','https://example.test'),('user','root;exit'),('port',0)]:
            with self.assertRaises(ToolError):connection_config(dict(base,**{field:value}))

    def test_native_password_not_saved_and_host_verification_required(self):
        with tempfile.TemporaryDirectory() as folder:
            config=connection_config({'host':'127.0.0.1','port':2222,'user':'root','auth':'password','sudo':False})
            ssh=SSH(config,folder);ssh.socket_directory=Path(folder)
            args=ssh.options()
            self.assertIn('StrictHostKeyChecking=ask',args);self.assertIn('ForwardAgent=no',args)
            self.assertIn('PubkeyAuthentication=no',args)
            self.assertFalse(any('password' in key for key in config))


if __name__=='__main__':unittest.main()
