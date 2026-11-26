"""Controller migration changes management metadata, never proxy grants."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'deploy/node'))
from api import InstallError
from installer import switch_panel,switch_plan
from system import Host
from test_installer import CONFIG,KEYS
from plan import inbounds


class SwitchController(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        cfg=dict(CONFIG,reality_port=12022,ws_port=18022)
        self.state={'config':cfg,'keys':KEYS,'node_api_id':7,'core_applied':True,'phase':'complete',
                    'desired_inbounds':inbounds(cfg,KEYS),'core_before':{'inbounds':[]}}
        self.new=dict(cfg,panel_url='https://new.example.test',panel_ip='1.1.1.1')
        self.host=Mock(root=self.root)
        (self.root/'data').mkdir();(self.root/'data/ssl_client_cert.pem').write_text('fixture certificate')
        self.remote=Mock()
        self.remote.certificate.return_value='fixture\ncertificate'
        self.remote.nodes.return_value=[{'id':7,'name':cfg['node_id'],'address':cfg['address'],
                                        'port':cfg['control_port'],'api_port':cfg['api_port'],'status':'connected'}]
        self.remote.core.return_value={'inbounds':copy.deepcopy(self.state['desired_inbounds'])}
    def tearDown(self):self.temp.cleanup()

    def test_same_identity_preserves_every_proxy_setting(self):
        result=switch_plan(self.remote,self.host,self.state,self.new)
        self.assertEqual({k:v for k,v in result.items() if k not in ('panel_ip','panel_url')},
                         {k:v for k,v in self.state['config'].items() if k not in ('panel_ip','panel_url')})
        self.remote.request.assert_not_called()

    def test_independent_panel_identity_rejected(self):
        self.remote.certificate.return_value='different certificate'
        with self.assertRaises(InstallError):switch_plan(self.remote,self.host,self.state,self.new)

    def test_missing_or_changed_node_rejected(self):
        for field,value in [('id',8),('address','9.9.9.9'),('port',62052)]:
            nodes=copy.deepcopy(self.remote.nodes.return_value);nodes[0][field]=value
            with patch.object(self.remote,'nodes',return_value=nodes):
                with self.assertRaises(InstallError):switch_plan(self.remote,self.host,self.state,self.new)

    def test_changed_inbound_and_staging_rejected(self):
        self.remote.core.return_value={'inbounds':[]}
        with self.assertRaises(InstallError):switch_plan(self.remote,self.host,self.state,self.new)
        self.remote.core.return_value={'inbounds':self.state['desired_inbounds']}
        self.remote.nodes.return_value[0]['status']='disabled'
        with self.assertRaises(InstallError):switch_plan(self.remote,self.host,self.state,self.new)

    def test_plan_no_local_mutations(self):
        before=copy.deepcopy(self.state)
        with patch('installer.ask',side_effect=[self.new['panel_url'],self.new['panel_ip']]),patch('installer.login',return_value=self.remote):
            switch_panel(self.host,self.state,True)
        self.assertEqual(before,self.state);self.host.switch_management.assert_not_called()
        self.assertFalse((self.root/'state.json').exists());self.remote.wait_node.assert_not_called()

    def test_interactive_switch_backups_and_read_only_api(self):
        original=copy.deepcopy(self.state)
        with patch('installer.ask',side_effect=[self.new['panel_url'],self.new['panel_ip'],'switch']),patch('installer.login',return_value=self.remote):
            switch_panel(self.host,self.state)
        self.assertEqual(json.loads(next((self.root/'releases').glob('panel-switch-*.json')).read_text()),original)
        saved=json.loads((self.root/'state.json').read_text())
        self.assertEqual(saved['config'],self.new)
        for field in ('keys','desired_inbounds','node_api_id','core_before'):self.assertEqual(saved[field],original[field])
        self.assertEqual(saved['panel_switch']['status'],'complete')
        self.remote.request.assert_not_called();self.host.start.assert_not_called()

    def test_only_owned_management_firewall_rules_replaced(self):
        cfg=self.new
        status=b'''[ 1] 62050/tcp ALLOW IN 8.8.4.4 # marzban-node-bootstrap
[ 2] 62051/tcp ALLOW IN 8.8.4.4 # marzban-node-bootstrap
[ 3] 443/tcp ALLOW IN 173.245.48.0/20 # marzban-node-bootstrap
[ 4] 22/tcp ALLOW IN Anywhere # marzban-node-bootstrap
[ 5] 62050/tcp ALLOW IN 9.9.9.9 # other-owner
'''
        with patch('system.run',return_value=status) as run:
            Host(str(self.root)).switch_management('8.8.4.4',cfg)
        deletes=[call.args[0] for call in run.call_args_list if 'delete' in call.args[0]]
        self.assertEqual(deletes,[['ufw','--force','delete','2'],['ufw','--force','delete','1']])


if __name__=='__main__':unittest.main()
