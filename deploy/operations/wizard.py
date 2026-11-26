"""Destination-side interactive migration with a separate, explicit cutover gate."""
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile

from api import public_ipv4
from bundle import seal,unseal,unpack
from common import MAX_BUNDLE,ToolError,digest,private_write,run,write_json
from database import inventory,same_data
import migrate
from panel import ask,install
from ssh_transport import SSH,connection_config

CUTOVER_PHASES={'stop_requested','source_stopped','final_saved','final_restored','activating'}


def prepare_target(host):
    state_path=host.root/'state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else None
    if state is None or (state['mode']=='fresh' and state['phase']!='active'):
        print('先在本机准备独立空面板和 HTTPS，完成后再连接旧服务器。',flush=True)
        install(host)
        state=json.loads(state_path.read_text()) if state_path.exists() else None
    if state is None or state['phase'] not in ('active','staging','activating'):
        raise ToolError('目标尚未就绪；若恢复事务中断，请先运行 marzban-migrate recover')
    if state['mode']=='fresh':
        profile=inventory(host.root/'data/db.sqlite3')
        if profile['tables']['users']['rows'] or profile['tables']['nodes']['rows']:
            raise ToolError('目标已有用户或节点，拒绝覆盖或连接源服务器')
    elif state['mode']!='migration':raise ToolError('目标部署类型不支持迁移')
    return state


def collect(previous=None):
    prior=previous or {}
    host=ask('旧服务器 SSH 地址（IP 或主机名）',prior.get('host',''))
    port=ask('旧服务器 SSH 端口',str(prior.get('port',22)))
    if not port.isdecimal():raise ToolError('SSH 端口必须为数字')
    user=ask('旧服务器 SSH 用户',prior.get('user','root'))
    default={'agent':'1','key':'2','password':'3'}.get(prior.get('auth'), '1' if os.environ.get('SSH_AUTH_SOCK') else '3')
    choice=ask('SSH 认证：1 Agent / 2 新服务器上的密钥文件 / 3 密码',default)
    if choice not in ('1','2','3'):raise ToolError('认证方式请选择 1、2 或 3')
    auth={'1':'agent','2':'key','3':'password'}[choice]
    key_path=ask('SSH 私钥 Linux 绝对路径',prior.get('key_path','')) if auth=='key' else ''
    sudo=ask('源端 Docker 是否需要免密 sudo，yes/no','yes' if prior.get('sudo') else 'no')
    if sudo not in ('yes','no'):raise ToolError('sudo 选项填写 yes 或 no')
    cfg=connection_config(dict(host=host,port=int(port),user=user,auth=auth,key_path=key_path,sudo=sudo=='yes'))
    address=ask('旧主控公网 IPv4（用于检查主控本机出口）',prior.get('address',host if re.fullmatch(r'[0-9.]+',host) else ''))
    cfg['address']=public_ipv4(address)
    domain=ask('旧面板完整域名，不带 https，可留空',prior.get('domain',''))
    if domain and not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',domain):raise ToolError('旧面板域名格式无效')
    cfg['domain']=domain
    return cfg


def read_bundle(path,password,expected=None):
    path=Path(path)
    if path.stat().st_size>MAX_BUNDLE+100:raise ToolError('迁移包超过大小限制')
    encrypted=path.read_bytes()
    if expected and digest(encrypted)!=expected:raise ToolError('迁移包与保存的记录不一致')
    return unseal(encrypted,password)


def validate_payload(payload,source,expected_fingerprint,identity=None,kind='preview',receipt=None):
    with tempfile.TemporaryDirectory(prefix='panel-validate-') as folder:
        manifest=unpack(payload,folder)
        same_data(manifest['profile'],inventory(Path(folder)/'data/db.sqlite3'))
    if manifest.get('kind')!=kind or manifest.get('source_container')!=source['id']:
        raise ToolError('迁移包类型或源容器身份不匹配')
    if manifest.get('app_fingerprint')!=expected_fingerprint:raise ToolError('源和目标应用版本不一致，拒绝隐式升级')
    if identity and manifest['profile']['identity']!=identity:raise ToolError('源面板的 JWT／节点 TLS 身份发生变化')
    if kind=='final' and (not manifest.get('source_stopped') or not receipt or manifest.get('stop_receipt')!=receipt):
        raise ToolError('最终包缺少本次有效停机收据')
    return manifest


def save_payload(directory,payload,password,kind):
    path=directory/(kind+'-'+secrets.token_hex(8)+'.mzb')
    encrypted=seal(payload,password);private_write(path,encrypted)
    return {'path':str(path),'sha256':digest(encrypted)}


def request_values(journal):
    cfg=journal['connection']
    return {'container':journal['source']['id'],'expected_id':journal['source']['id'],
            'migration_id':journal['migration_id'],'addresses':list(dict.fromkeys(filter(None,[cfg['address'],cfg['domain'],cfg['host']])))}


def preview(host,remote,journal,password,save,directory,fingerprint):
    if journal.get('phase') in CUTOVER_PHASES:raise ToolError('已进入正式切换，请选择 2 继续，不能重新做在线预演')
    print('[迁移] 1/3 在线一致性备份，通过 SSH 自动传输；旧主控继续运行',flush=True)
    payload=remote.request('export',**request_values(journal))
    manifest=validate_payload(payload,journal['source'],fingerprint,journal.get('identity'))
    bundle=save_payload(directory,payload,password,'preview')
    journal.update(preview=bundle,identity=manifest['profile']['identity'],phase='preview_saved')
    save()
    print('[迁移] 2/3 在新主控隔离恢复并逐表校验',flush=True)
    migrate.restore(host,bundle['path'],password,confirmed=True)
    journal['phase']='preview_restored';save()
    print('[迁移] 3/3 隔离验收就绪；可使用旧管理员检查用户、到期时间、流量和授权。',flush=True)
    print('旧主控仍运行，新主控不连接正式节点、不开放订阅。')
    if manifest['local_exits']['users']:
        print('源主控本机出口仍有用户授权，需要先安排这些线路，正式切换会阻止直接接管。')


def cutover(host,remote,journal,password,save,directory,fingerprint):
    if journal.get('phase') not in CUTOVER_PHASES|{'preview_restored'}:
        raise ToolError('请先完成在线备份与隔离验收')
    migrate.verify(host)
    values=request_values(journal)
    source=remote.request('inspect',**values)
    activation_resume=json.loads((host.root/'state.json').read_text())['phase']=='activating'
    if activation_resume and journal.get('phase')!='activating':raise ToolError('目标正在接管，但向导进度不一致，请人工核对')
    if journal['phase']=='preview_restored':
        if not source['running']:raise ToolError('源主控已停止且没有本次切换记录，请人工核对')
        # Refresh the source report before the outage, including current local-exit grants.
        payload=remote.request('export',**values)
        manifest=validate_payload(payload,journal['source'],fingerprint,journal['identity'])
        if manifest['local_exits']['users']:raise ToolError('源主控本机出口仍有用户授权，请先处理后再正式切换')
    elif journal.get('final'):
        payload=read_bundle(journal['final']['path'],password,journal['final']['sha256'])
        validate_payload(payload,journal['source'],fingerprint,journal['identity'],'final',journal.get('receipt'))
    else:
        # A wrong passphrase must not stop a still-running source during resume.
        read_bundle(journal['preview']['path'],password,journal['preview']['sha256'])
    phrase='cutover '+journal['source']['name']
    print('正式切换将停止旧主控，获取最新最终备份，恢复并启动新主控。')
    print('请先确认用户数据验收完成、各出口云和系统防火墙已准备新主控来源，旧主控没有其他自动启动任务。')
    if ask('确认停机接管，完整输入 '+phrase)!=phrase:
        print('未执行正式切换，旧主控状态未由此命令改变。');return
    if activation_resume:
        if remote.request('stopped',**values)!=journal.get('receipt'):raise ToolError('旧主控未保持本次停机状态')
        migrate.activate(host,confirmed=True)
        journal['phase']='complete';save()
        print('已继续完成接管，旧主控保持停止。');return
    journal['phase']='stop_requested';save()
    try:
        record=remote.request('freeze',**values)
        if record.get('migration_id')!=journal['migration_id'] or record.get('source_container')!=journal['source']['id'] or record.get('phase')!='stopped':
            raise ToolError('停机收据无效，未启动目标主控')
        journal.update(receipt=record,phase='source_stopped');save()
        print('[切换] 旧主控已停止，自动导出并传输最终快照',flush=True)
        payload=remote.request('final',**values)
        manifest=validate_payload(payload,journal['source'],fingerprint,journal['identity'],'final',record)
        if manifest['local_exits']['users']:raise ToolError('最终快照仍有源主控出口授权，拒绝接管')
        bundle=save_payload(directory,payload,password,'final')
        journal.update(final=bundle,phase='final_saved');save()
        migrate.restore(host,bundle['path'],password,confirmed=True)
        journal['phase']='final_restored';save()
        # Recheck over the live SSH session immediately before starting the new controller.
        if remote.request('stopped',**values)!=record:raise ToolError('源停机收据发生变化，拒绝启动新主控')
        journal['phase']='activating';save()
        migrate.activate(host,confirmed=True)
        journal['phase']='complete';save()
    except BaseException:
        print('切换尚未确认完成。旧主控可能已停止；不会自动重启。重新运行向导选择 2 按记录继续，保持唯一主控。',flush=True)
        raise
    print('正式接管完成。请逐台验收节点、用户订阅、实际出口 IP 与新增流量；旧主控保持停止。')


def wizard(host):
    target=prepare_target(host)
    directory=host.root/'migration';path=directory/'wizard.json'
    journal=json.loads(path.read_text()) if path.exists() else None
    if target['mode']=='migration' and target['phase']=='active':
        if journal and journal.get('phase')=='activating':
            # Activation may have completed just before the operator lost the session.
            if target.get('manifest',{}).get('stop_receipt')!=journal.get('receipt'):raise ToolError('目标接管身份与向导记录不匹配')
            journal['phase']='complete';write_json(path,journal)
        migrate.verify(host);print('目标已经接管，不会再次导入或操作旧主控。');return
    action=ask('迁移操作：1 在线备份并隔离恢复 / 2 正式停机切换 / 0 退出','1')
    if action=='0':return
    if action not in ('1','2'):raise ToolError('请选择 1、2 或 0')
    if action=='2' and not journal:raise ToolError('请先完成隔离验收')
    cfg=collect(journal.get('connection') if journal else None)
    password=getpass.getpass('迁移备份加密口令（至少12位，脚本不保存） > ')
    if len(password)<12 or password!=getpass.getpass('再次输入加密口令 > '):raise ToolError('备份口令不匹配或太短')
    if journal and journal.get('preview'):read_bundle(journal['preview']['path'],password,journal['preview']['sha256'])
    print('源 SSH：'+cfg['user']+'@'+cfg['host']+':'+str(cfg['port']))
    print('目标面板：https://'+target['config']['panel_domain']+'；订阅：https://'+target['config']['sub_domain'])
    print('SSH 连接后只上传临时通用工具；在线预演不停止旧主控。')
    if ask('确认连接源端并执行所选流程，输入 migrate')!='migrate':return
    if not shutil.which('ssh'):
        run(['apt-get','update']);run(['apt-get','install','-y','openssh-client'])
    host.save_toolkit()
    fingerprint=host.compose('run','--rm','-T','--no-deps','panel','python','/opt/panel-tools/exporter.py','fingerprint').decode().strip()
    if not re.fullmatch(r'[a-f0-9]{64}',fingerprint):raise ToolError('目标应用指纹无效')
    if journal is None:journal={'migration_id':secrets.token_hex(16),'phase':'planned'}
    journal['connection']=cfg
    save=lambda:write_json(path,journal)
    save()
    with SSH(cfg,directory) as remote:
        remote.stage()
        if not journal.get('source'):
            name=ask('旧面板容器名，留空列出可选面板')
            if not name:
                candidates=remote.request('containers')
                if not candidates:raise ToolError('未识别到面板容器，请重试并填写实际容器名')
                for index,item in enumerate(candidates,1):print(str(index)+'. '+item['name']+' ('+('运行中' if item['running'] else '已停止')+')')
                selected=ask('选择旧面板容器序号')
                if not selected.isdecimal() or not 1<=int(selected)<=len(candidates):raise ToolError('容器序号无效')
                name=candidates[int(selected)-1]['name']
            journal['source']=remote.request('inspect',container=name)
        source=journal['source']
        if not re.fullmatch(r'[a-f0-9]{64}',source.get('id','')) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',source.get('name','')):
            raise ToolError('源容器响应无效')
        destination=host.compose('ps','-q','panel').decode().strip()
        if len(destination)>=12 and source['id'].startswith(destination):raise ToolError('选择的是目标面板自身，拒绝迁移')
        save()
        values=request_values(journal)
        remote.request('inspect',**values)
        if remote.request('fingerprint',**values)['fingerprint']!=fingerprint:raise ToolError('源目标应用指纹不一致，未执行备份或停机')
        if action=='1':preview(host,remote,journal,password,save,directory,fingerprint)
        else:cutover(host,remote,journal,password,save,directory,fingerprint)
    password=''
    if action=='1':print('本次到隔离验收为止。以后重新运行 marzban-migrate，选择 2 才会进入停机确认。')
