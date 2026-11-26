"""An isolated, expiring account verifies the proxy and the panel's metering."""
import json
import secrets
import socket
import subprocess
import time

from api import APIError, InstallError
from plan import NODE_IMAGE


def client_config(cfg, keys, user_id, kind, socks_port):
    user = {"id": user_id, "encryption": "none"}
    if kind == "reality":
        # Loopback also works on cloud NATs without public-IP hairpin support.
        address, port = "127.0.0.1", cfg["reality_port"]
        stream = {"network": "tcp", "security": "reality", "realitySettings": {
            "serverName": cfg["sni"], "fingerprint": "chrome", "publicKey": keys["public"], "shortId": keys["short_id"]}}
    else:
        address, port = cfg["domain"], 443
        stream = {"network": "ws", "security": "tls", "tlsSettings": {"serverName": cfg["domain"]},
                  "wsSettings": {"path": "/marzban-ws", "headers": {"Host": cfg["domain"]}}}
    return {"log": {"loglevel": "none"},
            "inbounds": [{"listen": "127.0.0.1", "port": socks_port, "protocol": "socks"}],
            "outbounds": [{"protocol": "vless", "settings": {"vnext": [
                {"address": address, "port": port, "users": [user]}]}, "streamSettings": stream}]}


def docker_probe(cfg, keys, user_id, kind):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    name = "marzban-probe-" + secrets.token_hex(8)
    command = ["docker", "run", "--rm", "-i", "--name", name, "--network", "host", "--log-driver", "none",
               "--entrypoint", "/usr/local/bin/xray", NODE_IMAGE, "run", "-config", "stdin:"]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.stdin.write(json.dumps(client_config(cfg, keys, user_id, kind, port)).encode())
        proc.stdin.close()
        for _ in range(60):
            if proc.poll() is not None:
                raise InstallError("临时 Xray 客户端启动失败")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise InstallError("临时客户端监听超时")
        args = ["curl", "--fail", "--silent", "--show-error", "--noproxy", "", "--max-time", "20",
                "--socks5-hostname", f"127.0.0.1:{port}"]
        for _ in range(3):
            result = subprocess.run(args + ["https://api4.ipify.org"], capture_output=True, timeout=25)
            if result.returncode == 0 and result.stdout.decode().strip() == cfg["address"]:
                break
            time.sleep(2)
        else:
            raise InstallError(f"{kind} 出口检测失败，请检查目标连通性、云安全组或证书")
        result = subprocess.run(args + ["-o", "/dev/null", "-w", "%{http_code}", "https://example.com/"],
                                capture_output=True, timeout=25)
        if result.returncode or result.stdout != b"200":
            raise InstallError(f"{kind} 外部 HTTPS 检测失败")
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def verify(panel, cfg, keys, journal, save, runner=docker_probe, seconds=90):
    if not journal.get("probe_username"):
        journal["probe_username"] = "nodecheck_" + secrets.token_hex(6)
        journal["probe_note"] = "node-bootstrap " + secrets.token_hex(16)
        save()
    username = journal["probe_username"]
    path = "/api/user/" + username
    try:
        account = panel.request("GET", path)
    except APIError as error:
        if error.status != 404:
            raise
        account = None
    if account and account.get("note") != journal["probe_note"]:
        raise InstallError("测试账号名称冲突，已停止")
    if account and (account.get("status") != "active" or account.get("expire", 0) <= int(time.time())
                    or account["used_traffic"] >= account.get("data_limit", 0)):
        panel.request("DELETE", path)
        account = None
    tags = [item["tag"] for item in journal["desired_inbounds"]]
    usage_before = panel.usage(journal["node_api_id"])
    try:
        if not account:
            account = panel.request("POST", "/api/user", {
                "username": username, "note": journal["probe_note"], "status": "active",
                "proxies": {"vless": {"flow": ""}}, "inbounds": {"vless": tags},
                "expire": int(time.time()) + 900, "data_limit": 10 * 1024 * 1024})
        baseline = account["used_traffic"]
        ident = account["proxies"]["vless"]["id"]
        results = []
        for kind in ("reality", "ws"):
            if cfg["mode"] in ("both", kind):
                print("  [检测] " + kind + " 认证与 HTTPS 出口", flush=True)
                for attempt in range(3):
                    # Core restart is asynchronous in Marzban 0.8.4. Reapply this
                    # isolated account through the standard API after a transient failure.
                    panel.request("PUT", path, {"inbounds": {"vless": tags}})
                    try:
                        runner(cfg, keys, ident, kind)
                        break
                    except InstallError:
                        if attempt == 2:
                            raise
                        time.sleep(2)
                results.append(kind)
        deadline = time.monotonic() + seconds
        print("  [检测] 等待用户与目标节点的流量统计更新", flush=True)
        while time.monotonic() < deadline:
            account = panel.request("GET", path)
            usage_after = panel.usage(journal["node_api_id"])
            if account["used_traffic"] > baseline and usage_after > usage_before:
                result = {"protocols": results, "egress": cfg["address"],
                          "test_account_bytes": account["used_traffic"] - baseline,
                          "node_bytes": usage_after - usage_before,
                          "reality_public_entry": "requires an external client" if "reality" in results else None}
                journal["verification"] = result
                save()
                return result
            time.sleep(3)
        raise InstallError("代理请求已通过，但流量统计尚未确认；重新运行 check 继续验收")
    finally:
        # If POST timed out, GET still discovers the account and verifies ownership.
        try:
            current = panel.request("GET", path)
            if current.get("note") == journal["probe_note"]:
                panel.request("DELETE", path)
                journal.pop("probe_username", None)
                journal.pop("probe_note", None)
                save()
        except APIError as error:
            if error.status == 404:
                journal.pop("probe_username", None)
                journal.pop("probe_note", None)
                save()
            else:
                journal.pop("verification", None)
                save()
                raise InstallError("测试账号清理尚未确认，请重新运行 check；该账号会在 15 分钟内到期") from None
        except InstallError:
            # The journal retains the expiring account for cleanup on the next check.
            if "verification" in journal:
                journal.pop("verification")
                save()
            raise InstallError("测试账号清理尚未确认，请重新运行 check；该账号会在 15 分钟内到期") from None
