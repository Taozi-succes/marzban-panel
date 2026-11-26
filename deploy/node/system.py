"""Linux preparation, private state files, and the existing Compose/Certbot setup."""
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
from urllib.request import urlopen

from api import InstallError
from plan import NODE_IMAGE, NGINX_IMAGE


def run(args, data=None):
    try:
        result = subprocess.run(args, input=data, capture_output=True, timeout=900)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError(f"执行 {Path(args[0]).name} 失败或超时") from None
    if result.returncode:
        # Keep process output private: ACME and Xray commands may contain credentials.
        raise InstallError(f"执行 {Path(args[0]).name} 失败，退出码 {result.returncode}")
    return result.stdout


def write_private(path, data, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise InstallError("配置路径不能是符号链接")
    payload = data.encode() if isinstance(data, str) else data
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".next-")
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path, value):
    write_private(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def local_ports():
    ports = set()
    for name in ("tcp", "tcp6", "udp", "udp6"):
        path = Path("/proc/net")/name
        if not path.exists():
            continue
        for line in path.read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] in ("0A", "07"):
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def generate_keys():
    private = run(["openssl", "genpkey", "-algorithm", "X25519", "-outform", "DER"])
    public = run(["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"], private)
    if len(private) != 48 or len(public) != 44:
        raise InstallError("OpenSSL X25519 输出格式不正确")
    encode = lambda value: base64.urlsafe_b64encode(value[-32:]).decode().rstrip("=")
    return {"private": encode(private), "public": encode(public), "short_id": secrets.token_hex(8)}


def compose_config(cfg, root):
    root = str(root)
    node = {"image": NODE_IMAGE, "restart": "unless-stopped", "network_mode": "host",
            "environment": {"SERVICE_HOST": "0.0.0.0", "SERVICE_PORT": str(cfg["control_port"]),
                "XRAY_API_HOST": "0.0.0.0", "XRAY_API_PORT": str(cfg["api_port"]),
                "SERVICE_PROTOCOL": "rest", "SSL_CLIENT_CERT_FILE": "/var/lib/marzban-node/ssl_client_cert.pem"},
            "volumes": [root + "/data:/var/lib/marzban-node"],
            "logging": {"driver": "json-file", "options": {"max-size": "5m", "max-file": "2"}}}
    result = {"name": "marzban-exit", "services": {"node": node}}
    if cfg["mode"] in ("both", "ws"):
        domain = cfg["domain"]
        result["services"]["ingress"] = {"image": NGINX_IMAGE, "restart": "unless-stopped", "network_mode": "host",
            "volumes": [root + "/nginx.conf:/etc/nginx/conf.d/default.conf:ro",
                f"/etc/letsencrypt/live/{domain}:/etc/letsencrypt/live/{domain}:ro",
                f"/etc/letsencrypt/archive/{domain}:/etc/letsencrypt/archive/{domain}:ro"],
            "logging": node["logging"]}
    return result


def nginx_config(cfg):
    return '''server {
    listen 443 ssl default_server;
    ssl_reject_handshake on;
}
server {
    listen 443 ssl;
    server_name DOMAIN;
    ssl_certificate /etc/letsencrypt/live/DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/DOMAIN/privkey.pem;
    location = /marzban-ws {
        proxy_pass http://127.0.0.1:WS_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
    location / {
        add_header Cache-Control no-store always;
        return 404;
    }
}
'''.replace("DOMAIN", cfg["domain"]).replace("WS_PORT", str(cfg["ws_port"]))


class Host:
    def __init__(self, root):
        self.root = Path(root)
        if not re.fullmatch(r"/[a-zA-Z0-9/_-]+", str(self.root)):
            raise InstallError("安装目录应为不含空格的 Linux 绝对路径")

    def compose(self, *args):
        return run(["docker", "compose", "--project-directory", str(self.root), "-f", str(self.root/"compose.yaml"), *args])

    def dependencies(self):
        release = {}
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                release[key] = value.strip('"')
        allowed = {"debian": {"12", "13"}, "ubuntu": {"22.04", "24.04", "26.04"}}
        distro = release.get("ID", "")
        if release.get("VERSION_ID") not in allowed.get(distro, set()):
            raise InstallError("当前版本支持 Debian 12/13、Ubuntu 22.04/24.04/26.04")
        if run(["dpkg", "--print-architecture"]).strip() != b"amd64":
            raise InstallError("此固定镜像版本先支持 amd64，其他架构需要独立验收")
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", "ca-certificates", "curl", "openssl", "ufw", "iproute2",
             "certbot", "python3-certbot-dns-cloudflare"])
        docker_exists = bool(shutil.which("docker"))
        compose_exists = docker_exists and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
        if not compose_exists:
            Path("/etc/apt/keyrings").mkdir(parents=True, exist_ok=True, mode=0o755)
            key = urlopen(f"https://download.docker.com/linux/{distro}/gpg", timeout=30).read()
            write_private("/etc/apt/keyrings/docker.asc", key, 0o644)
            codename = release["VERSION_CODENAME"]
            source = (f"deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] "
                      f"https://download.docker.com/linux/{distro} {codename} stable\n")
            write_private("/etc/apt/sources.list.d/marzban-docker.list", source, 0o644)
            run(["apt-get", "update"])
            packages = ["docker-compose-plugin"] if docker_exists else ["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"]
            run(["apt-get", "install", "-y", *packages])
        run(["docker", "compose", "version"])
        run(["systemctl", "enable", "--now", "docker"])
        projects = run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=marzban-exit", "--format",
                        '{{.Label "com.docker.compose.project.working_dir"}}']).decode().splitlines()
        if any(Path(path).resolve() != self.root.resolve() for path in projects):
            raise InstallError("本机已有其他目录管理 marzban-exit 项目，请先核对部署归属")
        run(["docker", "pull", NODE_IMAGE])

    def prepare(self, cfg, certificate, cf_token):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        write_private(self.root/"data/ssl_client_cert.pem", certificate)
        write_private(self.root/"compose.yaml", json.dumps(compose_config(cfg, self.root), indent=2))
        if cfg["mode"] in ("both", "ws"):
            write_private(self.root/"nginx.conf", nginx_config(cfg))
            credentials = self.root/"credentials/cloudflare.ini"
            if not cf_token or re.search(r"\s", cf_token):
                raise InstallError("Cloudflare Token 格式不正确")
            write_private(credentials, "dns_cloudflare_api_token = " + cf_token + "\n")
            run(["certbot", "certonly", "--non-interactive", "--agree-tos", "--email", cfg["email"],
                 "--dns-cloudflare", "--dns-cloudflare-credentials", str(credentials),
                 "--dns-cloudflare-propagation-seconds", "45", "--no-directory-hooks",
                 "--cert-name", cfg["domain"], "-d", cfg["domain"], "--keep-until-expiring"])
            run(["docker", "pull", NGINX_IMAGE])
            self.compose("run", "--rm", "--no-deps", "-T", "ingress", "nginx", "-t")

    def firewall(self, cfg):
        label = "marzban-node-bootstrap"
        status = run(["ufw", "status", "numbered"]).decode()
        numbers = [int(m.group(1)) for line in status.splitlines()
                   if line.rstrip().endswith("# " + label) and (m := re.match(r"\[\s*(\d+)\]", line))]
        for number in sorted(numbers, reverse=True):
            run(["ufw", "--force", "delete", str(number)])
        ssh_port = os.environ.get("SSH_CONNECTION", "").split()
        ssh_ports = {int(ssh_port[-1])} if ssh_port else set()
        if not ssh_ports and shutil.which("sshd"):
            ssh_ports = {int(line.split()[1]) for line in run(["sshd", "-T"]).decode().splitlines() if line.startswith("port ")}
        if not ssh_ports:
            raise InstallError("无法识别 SSH 端口，请在 SSH 会话中安装")
        for port in sorted(ssh_ports):
            run(["ufw", "allow", str(port) + "/tcp", "comment", label])
        for port in (cfg["control_port"], cfg["api_port"]):
            run(["ufw", "insert", "1", "deny", str(port) + "/tcp", "comment", label])
            run(["ufw", "insert", "1", "allow", "from", cfg["panel_ip"], "to", "any", "port", str(port),
                 "proto", "tcp", "comment", label])
        if cfg["mode"] in ("both", "reality"):
            run(["ufw", "allow", str(cfg["reality_port"]) + "/tcp", "comment", label])
        if cfg["mode"] in ("both", "ws"):
            with urlopen("https://www.cloudflare.com/ips-v4/", timeout=25) as response:
                networks = response.read(16384).decode().splitlines()
            if not networks or any(not isinstance(ipaddress.ip_network(n), ipaddress.IPv4Network)
                                   or not ipaddress.ip_network(n).is_global or ipaddress.ip_network(n).prefixlen < 8 for n in networks):
                raise InstallError("Cloudflare 网段校验失败")
            run(["ufw", "insert", "1", "deny", "443/tcp", "comment", label])
            for network in networks:
                run(["ufw", "insert", "1", "allow", "from", network, "to", "any", "port", "443", "proto", "tcp", "comment", label])
        run(["ufw", "--force", "enable"])

    def start(self, cfg):
        self.compose("up", "-d", "--remove-orphans")
        if cfg["mode"] in ("both", "ws"):
            self.compose("exec", "-T", "ingress", "nginx", "-t")
            service = f'''[Unit]
Description=Renew the Marzban node HTTPS certificate
After=network-online.target docker.service
[Service]
Type=oneshot
WorkingDirectory={self.root}
ExecStart=/usr/bin/certbot renew --quiet --no-directory-hooks --cert-name {cfg["domain"]} --deploy-hook "/usr/bin/docker compose --project-directory {self.root} exec -T ingress nginx -s reload"
'''
            timer = '''[Unit]
Description=Schedule the Marzban node certificate renewal
[Timer]
OnCalendar=*-*-* 03,15:00:00
RandomizedDelaySec=3600
Persistent=true
[Install]
WantedBy=timers.target
'''
            write_private("/etc/systemd/system/marzban-node-renew.service", service, 0o644)
            write_private("/etc/systemd/system/marzban-node-renew.timer", timer, 0o644)
            run(["systemctl", "daemon-reload"])
            run(["systemctl", "enable", "--now", "marzban-node-renew.timer"])

    def install_cli(self):
        source = Path(__file__).parent
        destination = self.root/"installer"
        destination.mkdir(exist_ok=True, mode=0o700)
        for name in ("installer.py", "api.py", "plan.py", "system.py", "probe.py", "release.py", "install.sh", "SHA256SUMS"):
            if (source/name).resolve() != (destination/name).resolve():
                write_private(destination/name, (source/name).read_bytes(), 0o700 if name.endswith(".sh") else 0o600)
        script = '#!/bin/sh\nexec /usr/bin/python3 ' + str(destination/"installer.py") + ' --root ' + str(self.root) + ' "$@"\n'
        write_private("/usr/local/bin/marzban-node", script, 0o755)

    def status(self):
        print(self.compose("ps").decode())
