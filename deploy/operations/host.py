"""Portable Compose stack. Only the proxy exposes a public port."""
import json
import os
from pathlib import Path
import re
import shutil
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,urlopen

from api import Cloudflare
from common import NGINX_IMAGE,ToolError,private_write,root_path,run,write_json


def cloudflare_ranges():
    try:
        request=Request("https://api.cloudflare.com/client/v4/ips",headers={"User-Agent":"marzban-panel-operator","Accept":"application/json"})
        with urlopen(request,timeout=25) as response:data=json.load(response)
        import ipaddress
        rows=data['result']['ipv4_cidrs'] if data.get('success') is True else []
        if not isinstance(rows,list) or not 1<=len(rows)<=256:raise ValueError()
        networks=[ipaddress.ip_network(value) for value in rows]
        if any(not isinstance(n,ipaddress.IPv4Network) or not n.is_global or n.prefixlen<8 for n in networks):raise ValueError()
        return [str(n) for n in networks]
    except HTTPError as error:
        raise ToolError(f"Cloudflare 网段 API 返回 HTTP {error.code}，尚未调整防火墙") from None
    except (URLError,OSError,ValueError,KeyError,TypeError):
        raise ToolError("无法取得有效 Cloudflare IPv4 网段，尚未调整防火墙") from None


def compose_config(config,root,stage):
    root=str(root)
    panel={"image":config['image'],"restart":"unless-stopped","init":True,
        "env_file":[{"path":".env","format":"raw"}],
        "environment":{"PANEL_STAGE":"staging" if stage else "live"},
        "command":["python","/opt/panel-tools/runtime.py"],
        "ports":[f"127.0.0.1:{config['http_port']}:8000"],
        "volumes":[root+"/data:/var/lib/marzban",root+"/toolkit:/opt/panel-tools:ro"],
        "networks":["backend"] if stage else ["backend","controller"],
        "logging":{"driver":"json-file","options":{"max-size":"5m","max-file":"2"}}}
    if stage:
        panel['environment'].update(TELEGRAM_API_TOKEN="",WEBHOOK_ADDRESS="",DISCORD_WEBHOOK_URL="")
    if config.get('external_xray'):
        panel['volumes'].append(root+"/runtime/xray-current:/usr/local/bin/xray-current:ro")
    ingress={"image":NGINX_IMAGE,"restart":"unless-stopped","ports":["443:443"],
        "volumes":[root+"/nginx.conf:/etc/nginx/conf.d/default.conf:ro",
            f"/etc/letsencrypt/live/{config['panel_domain']}:/etc/letsencrypt/live/{config['panel_domain']}:ro",
            f"/etc/letsencrypt/archive/{config['panel_domain']}:/etc/letsencrypt/archive/{config['panel_domain']}:ro"],
        "networks":["backend","edge"],"logging":panel['logging']}
    return {"name":"marzban-panel","services":{"panel":panel,"ingress":ingress},
        "networks":{"backend":{"internal":True},"controller":{},"edge":{}}}


def nginx_config(config):
    panel,sub,path=config['panel_domain'],config['sub_domain'],config.get('sub_path','sub')
    # Docker-published ports can bypass UFW's INPUT chain. Enforce the origin
    # restriction in Nginx as well; never rely on UFW alone for this port.
    import ipaddress
    ranges=[str(ipaddress.IPv4Network(value)) for value in config.get('cloudflare_ranges',[])]
    access='\n'.join('allow '+value+';' for value in ranges)+'\ndeny all;\n'
    proxy='''proxy_pass http://panel:8000;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto https;
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 1h;
'''
    tls=f'''ssl_certificate /etc/letsencrypt/live/{panel}/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/{panel}/privkey.pem;
ssl_protocols TLSv1.2 TLSv1.3;
'''
    return f'''server {{ listen 443 ssl default_server; ssl_reject_handshake on; }}
server {{
listen 443 ssl; server_name {panel};
{tls}
{access}
location = /_panel_health {{ return 404; }}
location / {{ {proxy} }}
}}
server {{
listen 443 ssl; server_name {sub};
{tls}
{access}
location = /{path} {{ {proxy} }}
location ^~ /{path}/ {{ {proxy} }}
location / {{ return 404; }}
}}
'''


class Host:
    def __init__(self,root):self.root=root_path(root)

    def compose(self,*args,data=None):
        return run(['docker','compose','--project-directory',str(self.root),'-f',str(self.root/'compose.yaml'),*args],data=data)

    def dependencies(self):
        release=dict(line.split('=',1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
        release={k:v.strip('"') for k,v in release.items()}
        allowed={'debian':{'12','13'},'ubuntu':{'22.04','24.04','26.04'}}
        if release.get('VERSION_ID') not in allowed.get(release.get('ID'),set()):raise ToolError('支持 Debian 12/13、Ubuntu 22.04/24.04/26.04')
        if run(['dpkg','--print-architecture']).strip()!=b'amd64':raise ToolError('当前镜像仅支持 amd64')
        run(['apt-get','update'])
        run(['apt-get','install','-y','curl','ca-certificates','openssl','ufw','certbot','python3-certbot-dns-cloudflare','python3-cryptography'])
        if not shutil.which('docker'):
            Path('/etc/apt/keyrings').mkdir(parents=True,exist_ok=True)
            distro=release['ID']
            try:
                with urlopen(f'https://download.docker.com/linux/{distro}/gpg',timeout=30) as response:key=response.read()
            except OSError:raise ToolError('下载 Docker 官方签名失败') from None
            private_write('/etc/apt/keyrings/marzban-docker.asc',key,0o644)
            private_write('/etc/apt/sources.list.d/marzban-docker.list',f"deb [arch=amd64 signed-by=/etc/apt/keyrings/marzban-docker.asc] https://download.docker.com/linux/{distro} {release['VERSION_CODENAME']} stable\n",0o644)
            run(['apt-get','update'])
            run(['apt-get','install','-y','docker-ce','docker-ce-cli','containerd.io','docker-compose-plugin'])
        version=run(['docker','compose','version']).decode()
        match=re.search(r'v?(\d+)\.(\d+)\.(\d+)',version)
        if not match or tuple(map(int,match.groups()))<(2,30,0):raise ToolError('需要 Docker Compose 2.30 或更高版本')
        run(['systemctl','enable','--now','docker'])
        projects=run(['docker','ps','-a','--filter','label=com.docker.compose.project=marzban-panel','--format','{{.Label "com.docker.compose.project.working_dir"}}']).decode().splitlines()
        if any(Path(p).resolve()!=self.root.resolve() for p in projects):raise ToolError('已有其他目录管理 marzban-panel 项目，拒绝接管')

    def check_ports(self,config):
        import socket
        own=run(['docker','ps','--filter','label=com.docker.compose.project=marzban-panel','--format','{{.Names}}']).decode().splitlines()
        if own:return
        for port in (443,config['http_port']):
            with socket.socket() as sock:
                try:sock.bind(('0.0.0.0',int(port)))
                except OSError:raise ToolError(f'端口 {port} 已被其他服务占用，拒绝接管') from None

    def pull(self,image):
        run(['docker','pull',image])
        details=json.loads(run(['docker','image','inspect',image]))[0]
        repository=image.split('@')[0].rsplit(':',1)[0] if '@' not in image else image.split('@')[0]
        pinned=next((value for value in details.get('RepoDigests',[]) if value.startswith(repository+'@sha256:')),None)
        if not pinned:raise ToolError('未取得镜像不可变摘要')
        return pinned

    def save_toolkit(self):
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700);self.root.chmod(0o700)
        for path in Path(__file__).parent.glob('*.py'):
            target=self.root/'toolkit'/path.name
            if path.resolve()!=target.resolve():private_write(target,path.read_bytes())
        for command,entry in [('marzban-panel','panel.py'),('marzban-migrate','migrate.py')]:
            private_write('/usr/local/bin/'+command,'#!/bin/sh\nexec /usr/bin/python3 '+str(self.root/'toolkit'/entry)+' --root '+str(self.root)+' "$@"\n',0o755)

    def render(self,config,stage):
        write_json(self.root/'compose.yaml',compose_config(config,self.root,stage))
        private_write(self.root/'nginx.conf',nginx_config(config))

    def tls(self,config,token,state,save):
        if not token or any(c.isspace() for c in token):raise ToolError('Cloudflare Token 格式无效')
        cloud=Cloudflare('https://api.cloudflare.com/client/v4',token)
        zone=cloud.zone(config['zone'])
        for domain in (config['panel_domain'],config['sub_domain']):
            def record(ident):
                state.setdefault('dns_owned',{})[domain]=ident;save()
            cloud.ensure_dns(zone,domain,config['address'],state.get('dns_owned',{}).get(domain),record)
        credentials=self.root/'credentials/cloudflare.ini'
        private_write(credentials,'dns_cloudflare_api_token = '+token+'\n')
        run(['certbot','certonly','--non-interactive','--agree-tos','--email',config['email'],'--dns-cloudflare',
            '--dns-cloudflare-credentials',str(credentials),'--dns-cloudflare-propagation-seconds','45','--no-directory-hooks',
            '--cert-name',config['panel_domain'],'-d',config['panel_domain'],'-d',config['sub_domain'],'--keep-until-expiring','--expand'])

    def firewall(self,config):
        networks=cloudflare_ranges()
        connection=os.environ.get('SSH_CONNECTION','').split()
        if len(connection)!=4 or not connection[-1].isdecimal():raise ToolError('请从 SSH 会话安装，以保留当前管理端口')
        ssh_port=int(connection[-1]);label='marzban-panel-bootstrap'
        status=run(['ufw','status','numbered']).decode()
        rows=[int(m.group(1)) for line in status.splitlines() if line.rstrip().endswith('# '+label) and (m:=re.match(r'\[\s*(\d+)\]',line))]
        for ident in sorted(rows,reverse=True):run(['ufw','--force','delete',str(ident)])
        run(['ufw','allow',str(ssh_port)+'/tcp','comment',label])
        run(['ufw','insert','1','deny','443/tcp','comment',label])
        for network in networks:run(['ufw','insert','1','allow','from',network,'to','any','port','443','proto','tcp','comment',label])
        run(['ufw','--force','enable'])
        config['cloudflare_ranges']=networks

    def start(self):
        self.compose('up','-d','--force-recreate')
        deadline=time.monotonic()+60
        while time.monotonic()<deadline:
            try:
                import json as js
                port=js.loads((self.root/'state.json').read_text())['config']['http_port']
                with urlopen(f'http://127.0.0.1:{port}/_panel_health',timeout=3) as response:
                    if response.status==200:
                        self.compose('exec','-T','ingress','nginx','-t')
                        return
            except OSError:pass
            time.sleep(2)
        raise ToolError('面板尚未通过本机健康检查，请查看容器状态')

    def renewal(self,config):
        service=f'''[Unit]
Description=Renew panel HTTPS certificate
After=network-online.target docker.service
[Service]
Type=oneshot
WorkingDirectory={self.root}
ExecStart=/usr/bin/certbot renew --quiet --no-directory-hooks --cert-name {config['panel_domain']} --deploy-hook "/usr/bin/docker compose --project-directory {self.root} exec -T ingress nginx -s reload"
'''
        timer='[Unit]\nDescription=Renew panel certificate twice daily\n[Timer]\nOnCalendar=*-*-* 03,15:00:00\nRandomizedDelaySec=3600\nPersistent=true\n[Install]\nWantedBy=timers.target\n'
        private_write('/etc/systemd/system/marzban-panel-renew.service',service,0o644)
        private_write('/etc/systemd/system/marzban-panel-renew.timer',timer,0o644)
        run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','marzban-panel-renew.timer'])
