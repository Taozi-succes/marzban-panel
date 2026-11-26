"""Small standard-library clients for the existing Marzban and Cloudflare APIs."""
import ipaddress
import json
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler, ProxyHandler


class InstallError(RuntimeError):
    pass


class APIError(InstallError):
    def __init__(self, method, path, status):
        self.status = status
        super().__init__(f"API {method} {path.split('?')[0]} 返回 HTTP {status}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base, token="", context=None):
        self.base = base.rstrip("/")
        parts = urlsplit(self.base)
        local = parts.hostname in ("localhost", "127.0.0.1", "::1")
        if parts.scheme != "https" and not (parts.scheme == "http" and local):
            raise InstallError("API 地址必须使用 HTTPS，本机测试地址除外")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise InstallError("API 地址中不能包含账号、查询参数或片段")
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), HTTPSHandler(context=context or ssl.create_default_context()))

    def request(self, method, path, body=None, form=False):
        if not path.startswith("/") or path.startswith("//"):
            raise InstallError("无效的 API 路径")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
            data = (urlencode(body) if form else json.dumps(body)).encode()
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=25) as response:
                raw = response.read(8 * 1024 * 1024)
                return json.loads(raw) if raw else None
        except HTTPError as error:
            # Response bodies can contain passwords, keys or whole core configurations.
            raise APIError(method, path, error.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise InstallError(f"API {method} {path.split('?')[0]} 连接或响应校验失败") from None


class Panel(API):
    def login(self, username, password):
        result = self.request("POST", "/api/admin/token", {"username": username, "password": password}, form=True)
        self.token = result["access_token"]
        # These operations require the sudo admin scope, unlike /api/system.
        self.request("GET", "/api/node/settings")

    def core(self):
        return self.request("GET", "/api/core/config")

    def hosts(self):
        return self.request("GET", "/api/hosts")

    def nodes(self):
        return self.request("GET", "/api/nodes")

    def certificate(self):
        value = self.request("GET", "/api/node/settings")["certificate"]
        if "-----BEGIN CERTIFICATE-----" not in value or "PRIVATE KEY" in value:
            raise InstallError("面板节点信任证书格式不正确")
        return value

    def wait_node(self, node_id, seconds=75):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            node = self.request("GET", f"/api/node/{node_id}")
            if node["status"] == "connected":
                return node
            time.sleep(2)
        raise InstallError("节点尚未连接，请检查云安全组、面板来源 IP 和节点日志")

    def usage(self, node_id):
        rows = self.request("GET", "/api/nodes/usage")["usages"]
        return sum(row["uplink"] + row["downlink"] for row in rows if row["node_id"] == node_id)

    def users(self):
        result = []
        offset = 0
        while True:
            rows = self.request("GET", f"/api/users?offset={offset}&limit=100")["users"]
            result.extend(rows)
            if len(rows) < 100:
                return result
            offset += len(rows)


class Cloudflare(API):
    def zone(self, name):
        result = self.request("GET", "/zones?" + urlencode({"name": name}))
        if not result.get("success") or len(result.get("result", [])) != 1:
            raise InstallError("找不到指定 Cloudflare 区域，请核对区域名称和 Token 权限")
        return result["result"][0]["id"]

    def ensure_dns(self, zone_id, name, address, owned_record=None, save=None):
        prefix = f"/zones/{zone_id}/dns_records"
        result = self.request("GET", prefix + "?" + urlencode({"name": name}))
        if not result.get("success"):
            raise InstallError("Cloudflare DNS 查询失败")
        records = result["result"]
        if records:
            if len(records) != 1 or records[0]["type"] != "A":
                raise InstallError("同名 DNS 已有其他记录，需要先核对")
            record = records[0]
            if record["content"] == address and record.get("proxied") is True:
                return record["id"], owned_record == record["id"]
            if record["id"] != owned_record:
                raise InstallError("DNS 已指向其他地址，安装器将停止以免覆盖现有业务")
            result = self.request("PUT", prefix + "/" + record["id"],
                                  {"type": "A", "name": name, "content": address, "proxied": True, "ttl": 1})
        else:
            result = self.request("POST", prefix,
                                  {"type": "A", "name": name, "content": address, "proxied": True, "ttl": 1})
        if not result.get("success"):
            raise InstallError("Cloudflare DNS 写入失败")
        record_id = result["result"]["id"]
        if save:
            save(record_id)
        return record_id, True


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        raise InstallError("请输入有效的公网 IPv4 地址") from None
    if not address.is_global:
        raise InstallError("请输入公网 IPv4 地址")
    return str(address)
