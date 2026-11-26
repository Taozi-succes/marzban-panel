"""Validate interactive settings and merge only this node's standard API objects."""
import copy
import hashlib
import json
import re

from api import InstallError, public_ipv4

VERSION = "1.2.0"
NODE_IMAGE = "gozargah/marzban-node@sha256:f93791ee35aa98b3851b74e5556c33a1d4607caf3e10781f52f37add14ed974d"
NGINX_IMAGE = "nginx@sha256:901e593c95dc85f5a729f5765516321366ef4ac401821431d408897e95287d0a"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def hostname(value):
    if len(value) > 253 or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}", value):
        raise InstallError("域名格式不正确，请填写小写完整域名")
    return value


def validate(settings):
    cfg = copy.deepcopy(settings)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,23}", cfg["node_id"]):
        raise InstallError("节点标识应为 1–24 位小写字母、数字或连字符，并以字母开头")
    if not cfg["name"].strip() or any(ord(c) < 32 or c in "{}" for c in cfg["name"]):
        raise InstallError("节点显示名称不能为空或包含控制字符、花括号")
    cfg["address"] = public_ipv4(cfg["address"])
    cfg["panel_ip"] = public_ipv4(cfg["panel_ip"])
    if cfg["mode"] not in ("both", "reality", "ws"):
        raise InstallError("协议模式应为 both、reality 或 ws")
    if cfg["mode"] in ("both", "ws"):
        hostname(cfg["zone"])
        hostname(cfg["domain"])
        if not cfg["domain"].endswith("." + cfg["zone"]):
            raise InstallError("WS 域名必须属于指定 Cloudflare 区域")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", cfg["email"]):
            raise InstallError("请填写有效的证书通知邮箱")
    if cfg["mode"] in ("both", "reality"):
        hostname(cfg["sni"])
    for name in ("reality_port", "ws_port", "control_port", "api_port"):
        if cfg.get(name) is not None and (isinstance(cfg[name], bool) or not 1024 <= int(cfg[name]) <= 65535):
            raise InstallError("端口必须在 1024–65535 范围内")
        if cfg.get(name) is not None:
            cfg[name] = int(cfg[name])
    return cfg


def occupied_ports(core):
    ports = set()
    for inbound in core.get("inbounds", []):
        spec = str(inbound.get("port", ""))
        for part in spec.split(","):
            if part.isdecimal():
                ports.add(int(part))
            elif re.fullmatch(r"\d+-\d+", part):
                start, end = map(int, part.split("-"))
                if end - start > 65535:
                    raise InstallError("已有端口范围不正确")
                ports.update(range(start, end + 1))
    return ports


def allocate_ports(cfg, core, local_ports):
    cfg = copy.deepcopy(cfg)
    core_ports = occupied_ports(core)
    used = core_ports | set(local_ports) | {443, cfg["control_port"], cfg["api_port"]}
    if cfg["control_port"] == cfg["api_port"] or 443 in (cfg["control_port"], cfg["api_port"]):
        raise InstallError("管理端口、API 端口和 HTTPS 端口需要各自独立")
    if core_ports & {cfg["control_port"], cfg["api_port"]}:
        raise InstallError("管理端口与面板已有入站端口冲突")
    for key, start, active in (("reality_port", 12020, cfg["mode"] in ("both", "reality")),
                               ("ws_port", 18020, cfg["mode"] in ("both", "ws"))):
        if not active:
            cfg[key] = None
            continue
        requested = cfg.get(key)
        if requested is None:
            requested = next((port for port in range(start, 62000) if port not in used), None)
        if requested is None or requested in used:
            raise InstallError(f"{key} 端口已占用，请重新选择")
        cfg[key] = requested
        used.add(requested)
    return cfg


def inbounds(cfg, keys):
    result = []
    if cfg["mode"] in ("both", "reality"):
        result.append({"tag": cfg["node_id"] + "-reality", "listen": "0.0.0.0", "port": cfg["reality_port"],
                       "protocol": "vless", "settings": {"clients": [], "decryption": "none"},
                       "streamSettings": {"network": "tcp", "security": "reality", "realitySettings": {
                           "show": False, "dest": cfg["sni"] + ":443", "xver": 0, "serverNames": [cfg["sni"]],
                           "privateKey": keys["private"], "shortIds": [keys["short_id"]]}}})
    if cfg["mode"] in ("both", "ws"):
        result.append({"tag": cfg["node_id"] + "-ws", "listen": "127.0.0.1", "port": cfg["ws_port"],
                       "protocol": "vless", "settings": {"clients": [], "decryption": "none"},
                       "streamSettings": {"network": "ws", "security": "none", "wsSettings": {"path": "/marzban-ws"}}})
    return result


def hosts(cfg):
    result = {}
    if cfg["mode"] in ("both", "reality"):
        result[cfg["node_id"] + "-reality"] = [{"remark": cfg["name"] + " · REALITY", "address": cfg["address"],
            "port": cfg["reality_port"], "sni": cfg["sni"], "fingerprint": "chrome", "security": "inbound_default",
            "is_disabled": False, "allowinsecure": False}]
    if cfg["mode"] in ("both", "ws"):
        result[cfg["node_id"] + "-ws"] = [{"remark": cfg["name"] + " · WS", "address": cfg["domain"], "port": 443,
            "sni": cfg["domain"], "host": cfg["domain"], "path": "/marzban-ws", "security": "tls",
            "is_disabled": False, "allowinsecure": False}]
    return result


def merge_core(current, desired, previous=None):
    old = {item["tag"]: item for item in previous or []}
    wanted = {item["tag"]: item for item in desired}
    present = {item["tag"]: item for item in current.get("inbounds", [])}
    for tag in old.keys() | wanted.keys():
        if tag in present and present[tag] not in (old.get(tag), wanted.get(tag)):
            raise InstallError("入站标签已被其他配置使用或发生外部修改，请核对后重试")
    merged = copy.deepcopy(current)
    merged["inbounds"] = [item for item in merged.get("inbounds", []) if item["tag"] not in old and item["tag"] not in wanted]
    merged["inbounds"].extend(copy.deepcopy(desired))
    return merged


def hosts_match(actual, wanted):
    return all(len(actual.get(tag, [])) == len(rows) and all(
        all(row.get(key) == value for key, value in expected.items())
        for row, expected in zip(actual.get(tag, []), rows)) for tag, rows in wanted.items())


def publish(panel, desired, journal, save):
    """Recover an uncertain PUT by reading back; never retry a blind full replacement."""
    current = panel.core()
    expected = merge_core(journal["core_before"], desired, journal.get("previous_inbounds"))
    if fingerprint(current) == fingerprint(expected):
        journal["core_applied"] = True
        save()
    elif fingerprint(current) != fingerprint(journal["core_before"]):
        raise InstallError("面板核心在安装期间发生变化，本次发布已停止；运行 marzban-node reconfigure 核对新计划")
    else:
        try:
            panel.request("PUT", "/api/core/config", expected)
        except InstallError:
            if fingerprint(panel.core()) != fingerprint(expected):
                raise
        journal["core_applied"] = True
        save()
    wanted_hosts = journal["desired_hosts"]
    actual = panel.hosts()
    if "hosts_before" not in journal:
        journal["hosts_before"] = {tag: actual.get(tag, []) for tag in wanted_hosts}
        save()
    if hosts_match(actual, wanted_hosts):
        journal["hosts_applied"] = True
        save()
        return
    if any(actual.get(tag, []) != journal["hosts_before"][tag] for tag in wanted_hosts):
        raise InstallError("节点 Host 发生外部修改，请核对后重试")
    try:
        panel.request("PUT", "/api/hosts", wanted_hosts)
    except InstallError:
        actual = panel.hosts()
        if not hosts_match(actual, wanted_hosts):
            raise
    journal["hosts_applied"] = True
    save()


def rollback_core(panel, journal):
    """Withdraw only this release's panel objects, retaining unrelated changes and counters."""
    current = panel.core()
    wanted = journal["desired_inbounds"]
    present = {item["tag"]: item for item in current.get("inbounds", [])}
    prior = {item["tag"]: item for item in journal.get("previous_inbounds", [])}
    if any(present.get(item["tag"]) not in (item, prior.get(item["tag"])) for item in wanted):
        raise InstallError("入站已发生外部修改，自动回退停止")
    actual_hosts = panel.hosts()
    before_hosts = journal.get("hosts_before", {})
    for tag in prior:
        if tag in before_hosts and not hosts_match(actual_hosts, {tag: before_hosts[tag]}) and not hosts_match(
                actual_hosts, {tag: journal["desired_hosts"][tag]}):
            raise InstallError("节点 Host 已发生外部修改，自动回退停止")
    reduced = merge_core(current, journal.get("previous_inbounds", []), wanted)
    if reduced != current:
        try:
            panel.request("PUT", "/api/core/config", reduced)
        except InstallError:
            if fingerprint(panel.core()) != fingerprint(reduced):
                raise
    restore = {tag: rows for tag, rows in before_hosts.items() if tag in prior}
    if restore and not hosts_match(panel.hosts(), restore):
        try:
            panel.request("PUT", "/api/hosts", restore)
        except InstallError:
            if not hosts_match(panel.hosts(), restore):
                raise
