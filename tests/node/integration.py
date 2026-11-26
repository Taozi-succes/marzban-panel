"""Real Marzban, node, Xray and Nginx in a disposable Docker bridge network."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"deploy/node"))
from api import InstallError, Panel
from installer import apply
from plan import NODE_IMAGE, NGINX_IMAGE, allocate_ports, hosts, inbounds, publish, rollback_core
from probe import client_config, verify

PANEL_IMAGE = "gozargah/marzban@sha256:8e422c21997e5d2e3fa231eeff73c0a19193c20fc02fa4958e9368abb9623b8d"


def docker(*args, data=None, timeout=120):
    result = subprocess.run(["docker", *args], input=data, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Docker test operation failed: " + " ".join(args[:3]) + "\n" + result.stderr.decode(errors="replace")[-1500:])
    return result.stdout


def main():
    prefix = "node-installer-it-" + secrets.token_hex(5)
    names = {kind: prefix + "-" + kind for kind in ("panel", "node", "nginx", "probe")}
    network = prefix + "-net"
    stage = Path(tempfile.mkdtemp(prefix=prefix + "-"))
    panel = None
    journal = None
    try:
        docker("network", "create", network)
        for image in (PANEL_IMAGE, NODE_IMAGE, NGINX_IMAGE):
            docker("image", "inspect", image)
        (stage/"xray.json").write_text(json.dumps({"inbounds": [{"tag": "existing", "listen": "127.0.0.1", "port": 10001,
            "protocol": "vless", "settings": {"clients": [], "decryption": "none"}}],
            "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]}))
        docker("run", "--rm", "-v", str(stage) + ":/data", "--entrypoint", "openssl", NODE_IMAGE,
               "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "/data/origin.key", "-out", "/data/origin.crt",
               "-days", "1", "-subj", "/CN=ws.example.test", "-addext", "subjectAltName=DNS:ws.example.test")
        docker("run", "-d", "--name", names["panel"], "--network", network, "-p", "127.0.0.1::8000",
               "-v", str(ROOT) + ":/installer:ro", "-v", str(stage) + ":/data", "-w", "/code",
               "-e", "SQLALCHEMY_DATABASE_URL=sqlite:////data/db.sqlite3", "-e", "XRAY_JSON=/data/xray.json",
               "-e", "JOB_RECORD_NODE_USAGES_INTERVAL=2", "-e", "JOB_RECORD_USER_USAGES_INTERVAL=2",
               "-e", "JOB_CORE_HEALTH_CHECK_INTERVAL=10", "--entrypoint", "python", PANEL_IMAGE, "/installer/tests/node/panel_fixture.py")
        published_port = docker("port", names["panel"], "8000/tcp").decode().strip().rsplit(":", 1)[1]
        panel = Panel("http://127.0.0.1:" + published_port)
        for _ in range(45):
            if docker("inspect", "--format", "{{.State.Running}}", names["panel"]).strip() != b"true":
                raise RuntimeError("Disposable panel process exited")
            try:
                panel.login("fixture_admin", "fixture_password")
                break
            except InstallError:
                time.sleep(1)
        else:
            raise RuntimeError("Disposable panel failed startup")
        print("Real isolated panel API ready", flush=True)
        (stage/"node-data").mkdir()
        (stage/"node-data/ssl_client_cert.pem").write_text(panel.certificate())
        docker("run", "-d", "--name", names["node"], "--network", network,
               "-v", str(stage/"node-data") + ":/var/lib/marzban-node",
               "-e", "SSL_CLIENT_CERT_FILE=/var/lib/marzban-node/ssl_client_cert.pem", "-e", "SERVICE_PROTOCOL=rest", NODE_IMAGE)
        node = panel.request("POST", "/api/node", {"name": "fixture", "address": names["node"], "port": 62050,
            "api_port": 62051, "add_as_new_host": False, "usage_coefficient": 1})
        panel.wait_node(node["id"], seconds=60)
        keys_raw = docker("run", "--rm", "--entrypoint", "/usr/local/bin/xray", NODE_IMAGE, "x25519").decode()
        keys = dict(line.split(": ", 1) for line in keys_raw.splitlines() if ": " in line)
        keys = {"private": keys["Private key"], "public": keys["Public key"], "short_id": "0123456789abcdef"}
        cfg = allocate_ports({"node_id": "fixture", "name": "Fixture", "address": node["address"], "mode": "both",
            "domain": "ws.example.test", "sni": "ws.example.test", "control_port": 62050, "api_port": 62051,
            "reality_port": None, "ws_port": None}, panel.core(), set())
        desired = inbounds(cfg, keys)
        desired[0]["streamSettings"]["realitySettings"]["dest"] = names["panel"] + ":18443"
        docker("exec", "-d", names["panel"], "python", "/installer/tests/node/tls_fixture.py")
        journal = {"config": cfg, "keys": keys, "node_api_id": node["id"], "core_before": panel.core(),
                   "desired_inbounds": desired, "desired_hosts": hosts(cfg)}
        existing = panel.request("GET", "/api/user/fixture_existing")
        class PreparedHost:
            # Containers above supply the isolated host; never change the test machine.
            def dependencies(self): pass
            def prepare(self, *args): pass
            def firewall(self, *args): pass
            def start(self, *args): pass
        try:
            apply(panel, None, PreparedHost(), journal, lambda: None)
        except InstallError as error:
            assert "自动授予" in str(error), str(error)
        else:
            raise AssertionError("Unmodified upstream permission policy must be rejected")
        self_after = panel.request("GET", "/api/user/fixture_existing")
        for field in ("inbounds", "expire", "data_limit", "used_traffic"):
            assert self_after[field] == existing[field]
        assert [i["tag"] for i in panel.core()["inbounds"]] == ["existing"]
        print("Real upstream auto-grant policy rejected and rolled back; original account retained", flush=True)
        # Transport-only phase has no pre-existing accounts. The public image does
        # not include a private panel's explicit-grant patch; that contract is
        # exercised separately by the hermetic API fixture.
        panel.request("DELETE", "/api/user/fixture_existing")
        publish(panel, desired, journal, lambda: None)
        # A fixture CA replaces public ACME for this isolated transport test.
        nginx = '''events {}\nhttp { server { listen 443 ssl; server_name ws.example.test;
ssl_certificate /data/origin.crt; ssl_certificate_key /data/origin.key;
location /marzban-ws { proxy_pass http://127.0.0.1:WS_PORT;
proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade"; proxy_set_header Host $host; } } }'''.replace("WS_PORT", str(cfg["ws_port"]))
        (stage/"nginx.conf").write_text(nginx)
        docker("run", "-d", "--name", names["nginx"], "--network", "container:"+names["node"],
               "-v", str(stage) + ":/data:ro", "--entrypoint", "nginx", NGINX_IMAGE, "-c", "/data/nginx.conf", "-g", "daemon off;")
        def runner(config, pair, ident, kind):
            client = client_config(config, pair, ident, kind, 17890)
            if kind == "ws":
                client["outbounds"][0]["settings"]["vnext"][0]["address"] = "127.0.0.1"
                client["outbounds"][0]["streamSettings"]["tlsSettings"]["certificates"] = [
                    {"certificateFile": "/data/origin.crt", "usage": "verify"}]
            payload = json.dumps(client) + "\n" + names["panel"] + "\n"
            try:
                result = docker("run", "--rm", "-i", "--name", names["probe"], "--network", "container:"+names["node"],
                    "-v", str(ROOT) + ":/repo:ro", "-v", str(stage) + ":/data:ro", "--entrypoint", "python", NODE_IMAGE,
                    "/repo/tests/node/socket_probe.py", data=payload.encode(), timeout=45)
            except RuntimeError as error:
                raise InstallError(str(error)) from error
            assert b"authenticated proxy" in result
            print(kind + " real Xray transport and CA-verified HTTPS passed", flush=True)
        result = verify(panel, cfg, keys, journal, lambda: None, runner=runner, seconds=35)
        assert result["test_account_bytes"] > 0 and result["node_bytes"] > 0
        assert len(panel.users()) == 0
        print("Real metering increments and temporary-account cleanup passed", flush=True)
        rollback_core(panel, journal)
        assert [i["tag"] for i in panel.core()["inbounds"]] == ["existing"]
        print("Scoped panel rollback passed", flush=True)
    except BaseException:
        for kind in ("panel", "node", "nginx"):
            result = subprocess.run(["docker", "logs", "--tail", "25", names[kind]], capture_output=True)
            (stage/(kind+".log")).write_bytes(result.stdout+result.stderr)
        print("Private diagnostics retained at " + str(stage), flush=True)
        raise
    finally:
        if panel:
            panel.token = ""
        for name in reversed(list(names.values())):
            assert name.startswith(prefix)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)


if __name__ == "__main__":
    main()
