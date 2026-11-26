#!/usr/bin/env python3
"""Fresh installation only: this command never imports another panel's data."""
import argparse
import getpass
import json
import os
import sys
from urllib.request import urlopen

from api import InstallError,public_ipv4
from common import DEFAULT_IMAGE,NGINX_IMAGE,ToolError,canonical,operator_lock,private_write,render_env,validate_config,write_json
from host import Host


def ask(label,default="",secret=False):
    prompt=label+(" ["+str(default)+"]" if default and not secret else "")+" > "
    return (getpass.getpass(prompt) if secret else input(prompt).strip()) or default


def collect():
    try:
        with urlopen('https://api4.ipify.org',timeout=8) as response:address=public_ipv4(response.read(128).decode().strip())
    except OSError:address=""
    config={"panel_domain":ask('管理面板完整域名'),"sub_domain":ask('订阅完整域名'),
        "zone":ask('Cloudflare 区域'),"address":public_ipv4(ask('本服务器公网 IPv4',address)),
        "email":ask('证书通知邮箱'),"image":ask('公开镜像版本',DEFAULT_IMAGE),"http_port":18080,"sub_path":"sub"}
    return validate_config(config)


def empty_core():
    # XRayConfig needs an inbound. A loopback SOCKS listener is deliberately not a
    # Marzban user protocol and creates no assignable subscription or public port.
    return {"inbounds":[{"tag":"bootstrap-local","listen":"127.0.0.1","port":19001,
        "protocol":"socks","settings":{"auth":"noauth"}}],"outbounds":[{"tag":"DIRECT","protocol":"freedom"}]}


def base_environment(config):
    return {"SQLALCHEMY_DATABASE_URL":"sqlite:////var/lib/marzban/db.sqlite3",
        "XRAY_JSON":"/var/lib/marzban/xray.json","XRAY_SUBSCRIPTION_URL_PREFIX":"https://"+config['sub_domain'],
        "XRAY_SUBSCRIPTION_PATH":config.get('sub_path','sub'),"XRAY_EXECUTABLE_PATH":"/usr/local/bin/xray"}


def install(host,plan=False):
    state_path=host.root/'state.json'
    state=json.loads(state_path.read_text()) if state_path.exists() else None
    if state and state['mode']!='fresh':raise ToolError('这是迁移实例，请使用迁移工具管理')
    if state and state['phase']=='active':
        print('该面板已完成部署；不会重新初始化数据库。');status(host);return
    if state:
        config=state['config'];print('继续未完成的全新部署，不读取其他面板数据。')
    else:
        if host.root.exists() and any(host.root.iterdir()):raise ToolError('部署目录已有其他文件，拒绝接管')
        config=collect()
        state={"mode":"fresh","phase":"planned","config":config}
    print('管理入口 https://'+config['panel_domain']+'/dashboard/')
    print('订阅入口 https://'+config['sub_domain']+'/'+config.get('sub_path','sub')+'/...')
    print('数据库从零创建：没有旧用户、旧节点或旧管理员。')
    if plan:return
    username=ask('新管理员账号',state.get('admin_username','admin'))
    password=ask('新管理员密码（重试时填写相同密码）',secret=True)
    if len(password)<12:raise ToolError('管理员密码至少 12 位')
    credential=host.root/'credentials/cloudflare.ini'
    prior=credential.read_text().split('=',1)[1].strip() if credential.exists() else ''
    token=ask('Cloudflare 区域 DNS 编辑 Token，已有可回车沿用',prior,True)
    if ask('确认在本机全新部署，输入 yes')!='yes':return
    state['admin_username']=username
    save=lambda:write_json(state_path,state)
    save();host.save_toolkit()
    print('[部署] 1/5 系统依赖与镜像',flush=True)
    host.dependencies();host.check_ports(config)
    config['image']=host.pull(config['image']);host.pull(NGINX_IMAGE)
    state['phase']='initializing';save()
    private_write(host.root/'.env',render_env(base_environment(config)))
    if not (host.root/'data/xray.json').exists():private_write(host.root/'data/xray.json',canonical(empty_core()))
    host.render(config,True)
    print('[部署] 2/5 创建空数据库和管理员',flush=True)
    host.compose('run','--rm','-T','--no-deps','panel','alembic','upgrade','head')
    host.compose('run','--rm','-T','--no-deps','panel','python','/opt/panel-tools/initialize.py',data=canonical({'username':username,'password':password}))
    password=''
    from database import inventory
    profile=inventory(host.root/'data/db.sqlite3')
    if profile['tables']['users']['rows'] or profile['tables']['nodes']['rows']:
        raise ToolError('全新部署检测到用户或节点，拒绝当作空面板继续')
    print('[部署] 3/5 DNS 与双域名证书',flush=True)
    host.tls(config,token,state,save);token=''
    print('[部署] 4/5 防火墙与 HTTPS 入口',flush=True)
    host.firewall(config);save();host.render(config,False)
    state['phase']='starting';save();host.start();host.renewal(config)
    state['phase']='active';save()
    print('[部署] 5/5 全新主面板已就绪；用户和出口节点由你随后新增。')


def status(host):
    state=json.loads((host.root/'state.json').read_text())
    print(json.dumps({k:state[k] for k in ('mode','phase','config')},ensure_ascii=False,indent=2))
    if (host.root/'compose.yaml').exists():print(host.compose('ps').decode(errors='replace'))


def main(argv=None):
    parser=argparse.ArgumentParser(description='独立全新面板部署，不导入旧数据')
    parser.add_argument('--root',default='/opt/marzban-panel')
    parser.add_argument('command',nargs='?',default='install',choices=['install','status'])
    parser.add_argument('--plan',action='store_true')
    args=parser.parse_args(argv)
    if os.geteuid()!=0 and not args.plan:raise ToolError('请使用 root 或 sudo -i')
    host=Host(args.root)
    if args.command=='status':status(host)
    elif args.plan:install(host,True)
    else:
        with operator_lock():install(host)


if __name__=='__main__':
    try:main()
    except (ToolError,InstallError,OSError,ValueError,KeyboardInterrupt) as error:
        print('部署停止：'+(str(error) if isinstance(error,(ToolError,InstallError)) else type(error).__name__),file=sys.stderr)
        sys.exit(1)
