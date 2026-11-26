#!/usr/bin/env python3
"""Explicit migration commands; no production source is contacted automatically."""
import argparse
import copy
from datetime import datetime,timezone
import getpass
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile

from api import InstallError
from bundle import seal,unseal,unpack
from common import MAX_BUNDLE,ToolError,canonical,digest,operator_lock,private_write,render_env,run,write_json
from database import disable_nodes,inventory,restore_node_statuses,same_data
from host import Host
from panel import ask,base_environment


def manifest_only(payload):
    with tarfile.open(fileobj=io.BytesIO(payload),mode='r:gz') as archive:
        entry=archive.getmember('manifest.json')
        if not entry.isfile() or entry.size>4*1024*1024:raise ToolError('迁移清单无效')
        return json.load(archive.extractfile(entry))


def inspect_container(container,docker_command=('docker',)):
    value=json.loads(run([*docker_command,'inspect',container]))[0]
    if not value.get('Config',{}).get('Image'):raise ToolError('源容器信息无效')
    return value


def export_container(container,kind,addresses,docker_command=('docker',),receipt=None):
    info=inspect_container(container,docker_command)
    if kind=='final' and info['State']['Running']:raise ToolError('最终备份要求源主控已停止')
    metadata={'kind':kind,'source_container':info['Id'],'source_image':info['Config']['Image'],
        'source_stopped':not info['State']['Running'],'source_addresses':addresses,
        'created_utc':datetime.now(timezone.utc).isoformat()}
    if receipt is not None:metadata['stop_receipt']=receipt
    # Config.Env is used only in a root-only temporary file, never printed.
    values=dict(row.split('=',1) for row in info['Config']['Env'] if '=' in row)
    with tempfile.TemporaryDirectory(prefix='panel-export-') as folder:
        path=Path(folder)/'source.env';private_write(path,render_env(values))
        tool=Path(__file__).parent.resolve()
        return run([*docker_command,'run','--rm','--network','none','--volumes-from',container+':ro',
            '--env-file',str(path),'-v',str(tool)+':/operator:ro','--entrypoint','python',info['Image'],
            '/operator/exporter.py',canonical(metadata).decode()])


def backup(container,output,addresses,freeze=False,allow_local=False):
    output=Path(output)
    if output.exists() or output.is_symlink():raise ToolError('备份文件已存在，拒绝覆盖')
    password=getpass.getpass('迁移包加密口令（至少12位，脚本不保存） > ')
    if len(password)<12 or password!=getpass.getpass('再次输入加密口令 > '):raise ToolError('口令不匹配或太短')
    payload=export_container(container,'preview',addresses)
    report=manifest_only(payload)
    print('源用户数',report['profile']['tables']['users']['rows'],'；节点数',report['profile']['tables']['nodes']['rows'])
    if freeze:
        if report['local_exits']['users'] and not allow_local:
            raise ToolError('仍有授权指向源主控出口；先迁移这些线路，或明确使用 --ack-local-exits-reviewed')
        if ask('该命令将停止源面板，输入容器名称确认')!=container:return
        run(['docker','stop',container])
        try:payload=export_container(container,'final',addresses)
        except BaseException:
            print('源主控已停止，最终备份失败；不会自动重启，请确认未发生接管后手动恢复。',file=sys.stderr)
            raise
    encrypted=seal(payload,password);password=''
    private_write(output,encrypted)
    print('加密备份已保存',str(output),'sha256',digest(encrypted))
    if not freeze:print('这是预演快照，不能用于激活接管；正式切换需 freeze 最终备份。')


def restore(host,archive,password,confirmed=False):
    state_path=host.root/'state.json'
    state=json.loads(state_path.read_text())
    if state['phase'] not in ('active','staging'):raise ToolError('先完成独立部署，或恢复已有验收实例')
    current=inventory(host.root/'data/db.sqlite3')
    if state['mode']=='fresh' and (current['tables']['users']['rows'] or current['tables']['nodes']['rows']):
        raise ToolError('目标面板已有业务数据，拒绝覆盖')
    if state['mode']=='migration' and state['phase']!='staging':raise ToolError('已接管的面板不能重新覆盖恢复')
    if Path(archive).stat().st_size>MAX_BUNDLE+100:raise ToolError('迁移包超过大小上限')
    encrypted=Path(archive).read_bytes()
    payload=unseal(encrypted,password)
    with tempfile.TemporaryDirectory(prefix='.restore-',dir=host.root) as folder:
        incoming=Path(folder)
        manifest=unpack(payload,incoming)
        actual=inventory(incoming/'data/db.sqlite3');same_data(manifest['profile'],actual)
        if state['mode']=='migration' and state['manifest']['profile']['identity']!=manifest['profile']['identity']:
            raise ToolError('最终包与预演包不属于同一面板身份')
        probe=host.compose('run','--rm','-T','--no-deps','panel','python','/opt/panel-tools/exporter.py','fingerprint').decode().strip()
        if probe!=manifest['app_fingerprint']:raise ToolError('目标镜像与源面板数据代码不一致，拒绝隐式升级')
        config=copy.deepcopy(state['config'])
        environment=json.loads((incoming/'source.env.json').read_text())
        base=base_environment(config)
        base.pop('XRAY_EXECUTABLE_PATH')
        base.pop('XRAY_SUBSCRIPTION_PATH')
        environment.update(base)
        path=environment.get('XRAY_SUBSCRIPTION_PATH','sub')
        import re
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}',path):raise ToolError('订阅路径不在支持范围')
        config['sub_path']=path
        config['external_xray']=(incoming/'runtime/xray-current').exists()
        disable_nodes(incoming/'data/db.sqlite3')
        same_data(manifest['profile'],inventory(incoming/'data/db.sqlite3'))
        if not confirmed and ask('恢复会替换空面板管理员并进入隔离验收，输入 restore')!='restore':return False
        releases=host.root/'releases';releases.mkdir(mode=0o700,exist_ok=True)
        saved=Path(tempfile.mkdtemp(prefix='restore-',dir=releases))
        for name in ('.env','compose.yaml','nginx.conf','state.json','runtime/xray-current'):
            source=host.root/name
            if source.exists():private_write(saved/name,source.read_bytes(),0o700 if name.startswith('runtime/') else 0o600)
        checkpoint=copy.deepcopy(state)
        checkpoint.update(phase='restoring',recovery=str(saved.relative_to(host.root)))
        write_json(state_path,checkpoint)
        try:
            host.compose('stop','panel')
            (host.root/'data').rename(saved/'data')
            (incoming/'data').rename(host.root/'data')
            if config['external_xray']:
                private_write(host.root/'runtime/xray-current',(incoming/'runtime/xray-current').read_bytes(),0o700)
            private_write(host.root/'.env',render_env(environment))
            state.update(mode='migration',phase='staging',config=config,manifest=manifest,bundle_sha256=digest(encrypted))
            write_json(state_path,state);host.render(config,True);host.start()
            verify(host)
        except BaseException:
            # The previous destination is guaranteed to be an empty fresh panel
            # or an isolated preview. Recovery never restarts the source server.
            write_json(state_path,checkpoint)
            try:recover(host)
            except BaseException:print('目标恢复中断；请运行 marzban-migrate recover 恢复目标原状态。',file=sys.stderr)
            raise
    verify(host)
    print('隔离恢复完成：不会启动主控核心、连接正式节点或开放订阅。')
    return True


def recover(host):
    state_path=host.root/'state.json';state=json.loads(state_path.read_text())
    if state.get('phase')!='restoring':raise ToolError('没有未完成的目标恢复事务')
    saved=(host.root/state['recovery']).resolve()
    if saved.parent!=(host.root/'releases').resolve() or not saved.name.startswith('restore-'):
        raise ToolError('恢复事务路径无效')
    previous=json.loads((saved/'state.json').read_text())
    if not (previous['mode']=='fresh' or previous['phase']=='staging'):
        raise ToolError('拒绝自动恢复已经接管的目标')
    host.compose('stop','panel')
    if (saved/'data').exists():
        if (host.root/'data').exists():(host.root/'data').rename(saved/'failed-data')
        (saved/'data').rename(host.root/'data')
    for name in ('.env','compose.yaml','nginx.conf','runtime/xray-current'):
        if (saved/name).exists():private_write(host.root/name,(saved/name).read_bytes(),0o700 if name.startswith('runtime/') else 0o600)
    write_json(state_path,previous);host.start()
    print('已恢复目标原状态；源服务器未被操作。')


def verify(host):
    state=json.loads((host.root/'state.json').read_text())
    actual=inventory(host.root/'data/db.sqlite3')
    if state.get('manifest'):
        if state['phase']=='staging':same_data(state['manifest']['profile'],actual)
        elif state['manifest']['profile']['identity']!=actual['identity']:raise ToolError('管理身份与源备份不一致')
    print(json.dumps({'phase':state['phase'],'schema':actual['schema'],
        'counts':{name:value['rows'] for name,value in actual['tables'].items() if name in ('users','nodes','admins','allowed_inbounds_association')}},ensure_ascii=False))
    return actual


def activate(host,allow_local=False,confirmed=False):
    state_path=host.root/'state.json';state=json.loads(state_path.read_text())
    if state['mode']!='migration' or state['phase'] not in ('staging','activating'):raise ToolError('只能从隔离验收或未完成接管状态继续')
    manifest=state['manifest']
    if manifest['kind']!='final' or not manifest.get('source_stopped'):raise ToolError('需要源主控 freeze 生成的最终停机快照，预演包不能接管')
    if manifest['local_exits']['users'] and not allow_local:raise ToolError('源主控出口仍有用户授权，需要先处理或明确确认已审阅')
    verify(host)
    print('源容器标识',manifest['source_container'])
    print('请确认旧主控仍保持停止，节点云防火墙已准备新来源 IP。')
    if not confirmed and ask('输入源容器标识末12位，确认唯一主控接管')!=manifest['source_container'][-12:]:return False
    state['phase']='activating';write_json(state_path,state)
    host.compose('stop','panel')
    restore_node_statuses(host.root/'data/db.sqlite3',manifest['profile']['node_statuses'])
    host.render(state['config'],False)
    host.start();state['phase']='active';write_json(state_path,state)
    print('新主控已启动接管。请执行出口、用户授权、订阅与计量验收；旧面板保持停止。')
    return True


def main(argv=None):
    parser=argparse.ArgumentParser(description='独立迁移工具；备份、隔离恢复与正式接管分开')
    parser.add_argument('--root',default='/opt/marzban-panel')
    commands=parser.add_subparsers(dest='command')
    commands.add_parser('wizard',help='交互填写源 SSH，自动备份、传输和隔离恢复；切换另行确认')
    for name in ('backup','freeze'):
        p=commands.add_parser(name);p.add_argument('--container',required=True);p.add_argument('--output',required=True)
        p.add_argument('--source-address',action='append',default=[]);p.add_argument('--ack-local-exits-reviewed',action='store_true')
    p=commands.add_parser('restore');p.add_argument('--bundle',required=True)
    p=commands.add_parser('activate');p.add_argument('--ack-local-exits-reviewed',action='store_true')
    commands.add_parser('verify')
    commands.add_parser('recover')
    args=parser.parse_args(argv)
    if os.geteuid()!=0:raise ToolError('请使用 root')
    if args.command is None:args.command='wizard'
    if args.command in ('backup','freeze','restore','wizard'):
        import importlib.util
        if importlib.util.find_spec('cryptography') is None:
            print('[迁移] 安装加密依赖，不修改面板数据',flush=True)
            run(['apt-get','update']);run(['apt-get','install','-y','python3-cryptography'])
    with operator_lock():
        if args.command=='wizard':
            from wizard import wizard
            wizard(Host(args.root))
        elif args.command in ('backup','freeze'):
            backup(args.container,args.output,args.source_address,args.command=='freeze',args.ack_local_exits_reviewed)
        else:
            host=Host(args.root)
            if args.command=='restore':restore(host,args.bundle,getpass.getpass('迁移包解密口令 > '))
            elif args.command=='activate':activate(host,args.ack_local_exits_reviewed)
            elif args.command=='recover':recover(host)
            else:verify(host)


if __name__=='__main__':
    try:main()
    except (ToolError,InstallError,OSError,ValueError,KeyboardInterrupt,KeyError) as error:
        print('迁移停止：'+(str(error) if isinstance(error,(ToolError,InstallError)) else type(error).__name__),file=sys.stderr);sys.exit(1)
