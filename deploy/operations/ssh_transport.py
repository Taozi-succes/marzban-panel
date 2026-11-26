"""OpenSSH transport: native authentication prompts, pinned host keys, no passwords in state."""
import base64
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from common import MAX_BUNDLE,ToolError,run


def connection_config(value):
    cfg=dict(value)
    host=cfg['host']
    if not isinstance(host,str) or not host or host.startswith('-') or len(host)>253:
        raise ToolError('旧服务器 SSH 地址无效')
    try:ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?',host):
            raise ToolError('SSH 地址只填写 IP 或主机名，不包含协议、账号或命令') from None
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',cfg['user']):raise ToolError('SSH 用户名无效')
    if not 1<=int(cfg['port'])<=65535:raise ToolError('SSH 端口无效')
    cfg['port']=int(cfg['port'])
    if cfg['auth'] not in ('agent','key','password'):raise ToolError('SSH 认证方式无效')
    if cfg['auth']=='key':
        path=Path(cfg.get('key_path','')).expanduser()
        if not path.is_absolute() or not path.is_file() or path.is_symlink():
            raise ToolError('SSH 私钥必须是新服务器上已有的普通文件，填写 Linux 绝对路径')
        if path.stat().st_mode&0o077:raise ToolError('SSH 私钥权限应为 600，请先核对权限')
        cfg['key_path']=str(path)
    else:cfg['key_path']=''
    if cfg['auth']=='agent' and not os.environ.get('SSH_AUTH_SOCK'):
        raise ToolError('当前没有 SSH Agent；请使用 ssh -A 登录新服务器，或选择密钥文件／密码认证')
    if not isinstance(cfg.get('sudo',False),bool):raise ToolError('sudo 选项无效')
    return cfg


UPLOAD=r'''
import base64,hashlib,json,os,pathlib,re,sys
root=pathlib.Path(sys.argv[1])
if not re.fullmatch(r'/tmp/marzban-migrate\.[A-Za-z0-9]{8,16}',str(root)) or root.is_symlink():raise SystemExit(2)
value=json.loads(sys.stdin.buffer.read(4*1024*1024))
files={}
for name,text in value.items():
    if name!='SHA256SUMS' and not re.fullmatch(r'[a-z_]+\.py',name):raise SystemExit(2)
    data=base64.b64decode(text,validate=True)
    if len(data)>1024*1024:raise SystemExit(2)
    files[name]=data
checks=dict((line.split()[1],line.split()[0]) for line in files['SHA256SUMS'].decode().splitlines())
if set(checks)!=set(files)-{'SHA256SUMS'}:raise SystemExit(2)
if any(hashlib.sha256(files[n]).hexdigest()!=h for n,h in checks.items()):raise SystemExit(2)
for name,data in files.items():
    path=root/name
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(data)
'''

CLEANUP=r'''
import os,pathlib,re,sys
root=pathlib.Path(sys.argv[1])
if not re.fullmatch(r'/tmp/marzban-migrate\.[A-Za-z0-9]{8,16}',str(root)) or root.is_symlink():raise SystemExit(2)
if root.stat().st_uid!=os.geteuid():raise SystemExit(2)
files=list(root.iterdir())
if any(p.is_symlink() or not p.is_file() or (p.name!='SHA256SUMS' and not re.fullmatch(r'[a-z_]+\.py',p.name)) for p in files):raise SystemExit(2)
for path in files:path.unlink()
root.rmdir()
'''


class SSH:
    def __init__(self,config,directory):
        self.config=connection_config(config)
        self.directory=Path(directory)
        self.socket_directory=None;self.remote_directory=None

    def options(self):
        cfg=self.config
        options=['ssh','-F','/dev/null','-p',str(cfg['port']),'-S',str(self.socket_directory/'control'),
                 '-o','ConnectTimeout=12','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3',
                 '-o','ForwardAgent=no','-o','ForwardX11=no','-o','PermitLocalCommand=no',
                 '-o','ClearAllForwardings=yes','-o','ProxyCommand=none',
                 '-o','StrictHostKeyChecking=ask','-o','GlobalKnownHostsFile=/dev/null',
                 '-o','UserKnownHostsFile='+str(self.directory/'known_hosts')]
        if cfg['auth']=='key':options+=['-i',cfg['key_path'],'-o','IdentitiesOnly=yes','-o','PreferredAuthentications=publickey']
        elif cfg['auth']=='password':options+=['-o','PubkeyAuthentication=no','-o','PreferredAuthentications=keyboard-interactive,password']
        else:options+=['-o','PreferredAuthentications=publickey']
        return options

    @property
    def peer(self):return self.config['user']+'@'+self.config['host']

    def __enter__(self):
        if not shutil.which('ssh'):raise ToolError('新服务器缺少 openssh-client')
        self.directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.socket_directory=Path(tempfile.mkdtemp(prefix='panel-ssh-'))
        try:
            print('SSH 首次连接会显示主机指纹；请与旧服务器控制台核对后确认。密码／密钥口令由 SSH 自己提示。',flush=True)
            result=subprocess.run([*self.options(),'-M','-N','-f','-o','ControlPersist=120',self.peer],timeout=180)
            if result.returncode:raise ToolError('SSH 认证或主机指纹校验失败，未执行备份或停机')
            return self
        except BaseException:
            self.__exit__(None,None,None);raise

    def call(self,args,data=None):
        result=run([*self.options(),'-o','BatchMode=yes','-T',self.peer,shlex.join([str(a) for a in args])],data=data,error_prefix='MIGRATION_ERROR:')
        if len(result)>MAX_BUNDLE+100:raise ToolError('远端响应超过大小上限')
        return result

    def stage(self):
        directory=self.call(['mktemp','-d','/tmp/marzban-migrate.XXXXXXXX']).decode().strip()
        if not re.fullmatch(r'/tmp/marzban-migrate\.[A-Za-z0-9]{8,16}',directory):raise ToolError('远端临时目录响应无效')
        self.remote_directory=directory
        source=Path(__file__).parent
        files={p.name:p.read_bytes() for p in source.glob('*.py')}
        # Generate from the actual local pinned files, including newly added modules.
        files['SHA256SUMS']=''.join(hashlib.sha256(v).hexdigest()+'  '+n+'\n' for n,v in sorted(files.items())).encode()
        payload=json.dumps({n:base64.b64encode(v).decode() for n,v in files.items()}).encode()
        self.call(['python3','-B','-c',UPLOAD,directory],payload)

    def request(self,operation,**values):
        if self.remote_directory is None:raise ToolError('尚未准备迁移工具')
        args=['python3','-B',self.remote_directory+'/remote.py',operation]
        if self.config.get('sudo'):args.append('--sudo')
        result=self.call(args,json.dumps(values).encode())
        return result if operation in ('export','final') else json.loads(result)

    def __exit__(self,*unused):
        if self.remote_directory:
            try:self.call(['python3','-B','-c',CLEANUP,self.remote_directory])
            except (ToolError,OSError):print('远端临时工具清理未完成：'+self.remote_directory,flush=True)
        if self.socket_directory:
            try:subprocess.run([*self.options(),'-O','exit',self.peer],capture_output=True,timeout=20)
            except (OSError,subprocess.TimeoutExpired):pass
            try:self.socket_directory.rmdir()
            except OSError:pass
