#!/usr/bin/env python3
"""Interactive installation using only the existing Marzban API and stock node image."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.request import urlopen

from api import Cloudflare, InstallError, Panel, public_ipv4
from plan import VERSION, allocate_ports, hosts, inbounds, merge_core, publish, rollback_core, validate
from probe import verify
from system import Host, generate_keys, local_ports, save_json


def ask(label, default="", secret=False):
    suffix = f" [{default}]" if default and not secret else ""
    value = getpass.getpass(label + suffix + " > ") if secret else input(label + suffix + " > ").strip()
    return value or default


def collect(previous=None):
    old = previous or {}
    print("Marzban 节点安装器 " + VERSION)
    cfg = {"panel_url": ask("面板 HTTPS 地址", old.get("panel_url", "")),
           "panel_ip": ask("面板管理服务器公网 IPv4", old.get("panel_ip", "")),
           "node_id": ask("节点标识，例如 us04", old.get("node_id", "")),
           "name": ask("节点显示名称", old.get("name", ""))}
    detected = old.get("address", "")
    if not detected:
        try:
            with urlopen("https://api4.ipify.org", timeout=8) as response:
                detected = public_ipv4(response.read(128).decode().strip())
        except (OSError, InstallError):
            detected = ""
    cfg["address"] = ask("本节点入站公网 IPv4，可覆盖自动检测", detected)
    cfg["mode"] = ask("协议模式 both / reality / ws", old.get("mode", "both"))
    cfg["sni"] = ask("REALITY 目标域名", old.get("sni", "www.cloudflare.com")) if cfg["mode"] != "ws" else ""
    if cfg["mode"] != "reality":
        cfg["zone"] = ask("Cloudflare 区域，例如 example.com", old.get("zone", ""))
        cfg["domain"] = ask("WS 完整域名", old.get("domain", cfg["node_id"] + "-ws." + cfg["zone"]))
        cfg["email"] = ask("证书通知邮箱", old.get("email", ""))
    else:
        cfg.update(zone="", domain="", email="")
    for name, label, default in (("control_port", "节点管理端口", 62050), ("api_port", "Xray API 端口", 62051),
                                  ("reality_port", "REALITY 端口，留空自动分配", ""),
                                  ("ws_port", "WS 本地端口，留空自动分配", "")):
        text = ask(label, str(old.get(name) or default))
        if text and not text.isdecimal():
            raise InstallError("端口必须为数字")
        cfg[name] = int(text) if text else None
    return validate(cfg)


def login(cfg):
    panel = Panel(cfg["panel_url"])
    username = ask("面板管理员账号")
    password = ask("面板管理员密码", secret=True)
    try:
        panel.login(username, password)
    finally:
        password = ""
    return panel


def prepare_plan(panel, cfg, previous=None, occupied=None):
    current = panel.core()
    old_inbounds = previous.get("desired_inbounds", []) if previous else []
    if previous:
        prior = previous["config"]
        if (cfg["node_id"], cfg["panel_url"], cfg["mode"]) != (prior["node_id"], prior["panel_url"], prior["mode"]):
            raise InstallError("已有节点的标识、面板和协议模式需保持一致；请为新接入创建独立节点")
        for key in ("reality_port", "ws_port", "control_port", "api_port"):
            if cfg[key] != prior[key]:
                raise InstallError("已有线路端口请在迁移时单独调整，以保留当前用户权限")
        # Existing managed listeners are permitted; other listener conflicts remain errors.
        cfg = validate(cfg)
        keys = previous["keys"]
    else:
        cfg = allocate_ports(cfg, current, occupied if occupied is not None else local_ports())
        keys = generate_keys() if cfg["mode"] in ("both", "reality") else {}
        for port in (cfg["control_port"], cfg["api_port"]):
            if port in (occupied if occupied is not None else local_ports()):
                raise InstallError("管理端口已被现有服务占用，请选择其他端口")
        if cfg["mode"] != "reality" and 443 in (occupied if occupied is not None else local_ports()):
            raise InstallError("443 已被现有服务占用，需要先规划 HTTPS 入口")
    desired = inbounds(cfg, keys)
    merged = merge_core(current, desired, old_inbounds)
    matches = [n for n in panel.nodes() if n["name"] == cfg["node_id"] or n["address"] == cfg["address"]]
    if any(not previous or n["id"] != previous.get("node_api_id") for n in matches):
        raise InstallError("面板已有相同名称或地址的节点，请核对接管范围")
    node = matches[0] if matches else None
    journal = {"release_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4),
            "version": VERSION, "phase": "planned", "config": cfg, "keys": keys, "core_before": current,
            "previous_inbounds": old_inbounds, "desired_inbounds": desired, "desired_hosts": hosts(cfg),
            "core_changes": merged != current,
            "node_api_id": node["id"] if node else None,
            "dns_record": previous.get("dns_record") if previous else None,
            "dns_owned": previous.get("dns_owned", False) if previous else False}
    if previous:
        for key in ("probe_username", "probe_note"):
            if key in previous:
                journal[key] = previous[key]
    return journal


def summary(journal):
    cfg = journal["config"]
    print("\n安装计划")
    for title, value in (("节点", cfg["node_id"]), ("公网地址", cfg["address"]), ("协议", cfg["mode"]),
                         ("管理来源", cfg["panel_ip"]), ("管理端口", cfg["control_port"]),
                         ("API 端口", cfg["api_port"]), ("REALITY 端口", cfg["reality_port"]),
                         ("WS 域名", cfg["domain"]), ("WS 本地端口", cfg["ws_port"])):
        if value:
            print(f"  {title}  {value}")
    print("  新入站默认无人分配，安装后在面板勾选用户权限。")
    print("  请在云安全组允许公网 REALITY 端口、443，以及面板来源的管理端口。")
    if journal["core_changes"]:
        print("  本次核心配置发布会使主控和已连接节点的 Xray 短暂重连。")
    print("  多台节点请依次安装。当前面板的全量配置 API 没有原子条件写入接口。")


def apply(panel, cloud, host, journal, save):
    def step(name, message):
        journal["step"] = name
        save()
        print("\n[安装] " + message, flush=True)
    cfg = journal["config"]
    step("dependencies", "1/8 安装或检查系统依赖、Docker 与节点镜像")
    host.dependencies()
    cf_token = cloud.token if cloud else ""
    if cloud:
        step("dns", "2/8 检查 Cloudflare 区域及 DNS 记录")
        zone_id = cloud.zone(cfg["zone"])
        journal["zone_api_id"] = zone_id
        save()

        def record(record_id):
            journal.update(dns_record=record_id, dns_owned=True)
            save()

        record_id, owned = cloud.ensure_dns(zone_id, cfg["domain"], cfg["address"],
                                           journal.get("dns_record") if journal.get("dns_owned") else None, record)
        journal.update(dns_record=record_id, dns_owned=owned)
        save()
    step("local_config", "3/8 准备节点信任、证书和本地入口配置")
    host.prepare(cfg, panel.certificate(), cf_token)
    step("firewall", "4/8 获取并校验 Cloudflare 网段、配置系统防火墙")
    host.firewall(cfg)
    step("services", "5/8 启动节点和 HTTPS 入口")
    host.start(cfg)
    journal["phase"] = "local_ready"
    save()
    step("node_registration", "6/8 注册或核对面板节点，等待连接")
    if journal.get("node_api_id") is None:
        existing = [n for n in panel.nodes() if n["name"] == cfg["node_id"] or n["address"] == cfg["address"]]
        expected = {"name": cfg["node_id"], "address": cfg["address"], "port": cfg["control_port"], "api_port": cfg["api_port"]}
        if existing and (not journal.get("registration_pending") or len(existing) != 1 or
                         any(existing[0].get(k) != value for k, value in expected.items())):
            raise InstallError("注册结果待核对，请检查面板中的同名节点后恢复安装记录")
        if existing:
            node = existing[0]
        else:
            journal["registration_pending"] = True
            save()
            try:
                node = panel.request("POST", "/api/node", dict(expected, usage_coefficient=1, add_as_new_host=False))
            except InstallError:
                existing = [n for n in panel.nodes() if n["name"] == cfg["node_id"] and
                            all(n.get(k) == value for k, value in expected.items())]
                if len(existing) != 1:
                    raise
                node = existing[0]
        journal["node_api_id"] = node["id"]
        save()
    else:
        current = panel.request("GET", f"/api/node/{journal['node_api_id']}")
        if current["address"] != cfg["address"] or current["port"] != cfg["control_port"] or current["api_port"] != cfg["api_port"]:
            panel.request("PUT", f"/api/node/{journal['node_api_id']}", {"address": cfg["address"],
                "port": cfg["control_port"], "api_port": cfg["api_port"]})
        elif current["status"] != "connected":
            panel.request("POST", f"/api/node/{journal['node_api_id']}/reconnect")
    panel.wait_node(journal["node_api_id"])
    new_tags = {item["tag"] for item in journal["desired_inbounds"]} - {
        item["tag"] for item in journal["core_before"].get("inbounds", [])}
    step("core_publish", "7/8 发布入站和 Host，检查原用户权限")
    publish(panel, journal["desired_inbounds"], journal, save)
    # Refuse a panel that still grants every user all newly created inbounds.
    if new_tags:
        for user in panel.users():
            selected = {tag for tags in user.get("inbounds", {}).values() for tag in tags}
            if selected & new_tags:
                rollback_core(panel, journal)
                journal["phase"] = "permission_policy_failed"
                save()
                raise InstallError("面板自动授予了新入站权限，本次入站已撤回；请使用显式入站分配版本")
    panel.wait_node(journal["node_api_id"])
    journal["phase"] = "published"
    save()


def run_cli(argv=None):
    parser = argparse.ArgumentParser(description="交互式接入 Marzban 标准节点")
    parser.add_argument("command", nargs="?", choices=("install", "status", "check", "reconfigure", "upgrade", "rollback"), default="install")
    parser.add_argument("--root", default="/opt/marzban-exit")
    parser.add_argument("--plan", action="store_true", help="仅生成计划，不安装或发布")
    parser.add_argument("--release", default="v" + VERSION, help="upgrade 使用的安装器 Git 标签")
    args = parser.parse_args(argv)
    if args.plan and args.command not in ("install", "reconfigure"):
        parser.error("--plan 只适用于 install 或 reconfigure")
    if args.plan or args.command in ("status", "upgrade"):
        return execute(args)
    if os.geteuid() != 0:
        raise InstallError("请使用 root 或 sudo 运行")
    import fcntl
    lock_path = Path("/run/lock/marzban-node-installer.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("本机已有安装或检查任务在运行") from None
        return execute(args)


def execute(args):
    if args.command != "status" and os.geteuid() != 0 and not args.plan:
        raise InstallError("请使用 root 或 sudo 运行")
    host = Host(args.root)
    state_path = host.root/"state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else None
    if args.command == "status":
        if not state:
            raise InstallError("没有本安装器的节点记录")
        summary(state)
        print("  当前阶段  " + state["phase"])
        print("  当前步骤  " + state.get("step", "旧版未记录具体步骤"))
        host.status()
        return
    if args.command == "upgrade":
        from release import upgrade
        upgrade(args.release, host.root)
        return
    if args.command in ("check", "rollback"):
        if not state:
            raise InstallError("没有本安装器的节点记录")
        validate(state["config"])
        panel = login(state["config"])
        save = lambda: save_json(state_path, state)
        try:
            if args.command == "rollback":
                if ask("撤回本次核心配置变更会短暂重连，输入 rollback 确认") != "rollback":
                    return
                rollback_core(panel, state)
                state["phase"] = "rolled_back"
                state["core_applied"] = False
                save()
                print("本次面板入站和 Host 变更已撤回，用户流量和配额记录继续保留。")
                print("节点服务、DNS 和证书保留供排查；此命令不卸载服务器。")
            else:
                if not state.get("core_applied") or not state.get("node_api_id"):
                    raise InstallError("节点尚未发布，请先继续安装")
                print("[检测] 验证代理认证、出口和流量统计，结束后清理测试账号", flush=True)
                result = verify(panel, state["config"], state["keys"], state, save)
                state["phase"] = "complete"
                save()
                print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            panel.token = ""
        return
    if state and args.command != "reconfigure" and state["phase"] not in ("complete", "rolled_back", "permission_policy_failed"):
        print("发现未完成的安装记录，将按原计划继续。")
        journal = state
        cfg = validate(journal["config"])
        panel = login(cfg)
    else:
        if host.root.exists() and any(host.root.iterdir()) and not state:
            raise InstallError("安装目录已有其他部署，请先核对并选择独立目录")
        cfg = collect(state["config"] if state else None)
        panel = login(cfg)
        journal = prepare_plan(panel, cfg, state)
    cloud = None
    try:
        cfg = journal["config"]
        if cfg["mode"] in ("both", "ws"):
            credential = host.root/"credentials/cloudflare.ini"
            token = ""
            if credential.exists():
                token = credential.read_text().split("=", 1)[1].strip()
            token = ask("Cloudflare DNS 编辑 Token，已有凭据可回车沿用", token, secret=True)
            cloud = Cloudflare("https://api.cloudflare.com/client/v4", token)
            cloud.zone(cfg["zone"])
        summary(journal)
        if args.plan:
            return
        if ask("确认安装并发布，输入 yes") != "yes":
            return
        host.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        host.root.chmod(0o700)
        if state and state["release_id"] != journal["release_id"]:
            save_json(host.root/"releases"/(state["release_id"] + ".json"), state)
        save = lambda: save_json(state_path, journal)
        journal["version"] = VERSION
        save()
        host.install_cli()
        apply(panel, cloud, host, journal, save)
        journal["step"] = "verification"
        save()
        print("\n[安装] 8/8 验证双协议、出口和流量统计，并清理测试账号", flush=True)
        result = verify(panel, cfg, journal["keys"], journal, save)
        journal["phase"] = "complete"
        journal["step"] = "complete"
        save()
        print("\n节点已接入，测试账号已清理。请在面板给用户勾选新入站。")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if cfg["mode"] != "ws":
            print("REALITY 已完成本机认证及出口检测，公网入口请再从外部客户端验证。")
    finally:
        panel.token = ""
        if cloud:
            cloud.token = ""

if __name__ == "__main__":
    try:
        run_cli()
    except (InstallError, KeyboardInterrupt, OSError, json.JSONDecodeError) as error:
        reason = str(error) if isinstance(error, InstallError) else (
            "用户取消" if isinstance(error, KeyboardInterrupt) else
            "系统或网络操作失败（" + type(error).__name__ + "），请核对上方安装步骤")
        print("\n安装器停止  " + reason, file=sys.stderr)
        print("已有安装记录可通过相同命令继续，详细状态使用 marzban-node status。", file=sys.stderr)
        sys.exit(1)
