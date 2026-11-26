"""Hermetic installer regression tests. The HTTP servers bind only to loopback."""
import base64
import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile
import threading
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy/node"))
from api import API, Cloudflare, InstallError, Panel, public_ipv4
from installer import apply, execute, prepare_plan, run_cli
from plan import allocate_ports, hosts, inbounds, publish, rollback_core, validate
from probe import client_config, verify
from release import extract_bundle
from system import Host, compose_config, generate_keys, nginx_config, save_json, write_private


KEYS = {"private": base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
        "public": base64.urlsafe_b64encode(bytes(range(32, 64))).decode().rstrip("="), "short_id": "0123456789abcdef"}
CONFIG = {"panel_url": "https://panel.example.test", "panel_ip": "8.8.4.4", "node_id": "us04", "name": "美国-04",
          "address": "8.8.8.8", "mode": "both", "sni": "www.cloudflare.com", "zone": "example.test",
          "domain": "us04-ws.example.test", "email": "owner@example.test", "control_port": 62050,
          "api_port": 62051, "reality_port": None, "ws_port": None}


class HTTPFixture:
    def __init__(self):
        self.core = {"inbounds": [{"tag": "old", "port": 10443, "protocol": "vless", "settings": {"clients": []}}],
                     "outbounds": [{"tag": "DIRECT", "protocol": "freedom"}]}
        self.hosts = {"old": [{"remark": "old", "address": "old.example.test"}]}
        self.users = {"existing": {"username": "existing", "expire": 2100000000, "data_limit": 1000,
                                  "used_traffic": 37, "inbounds": {"vless": ["old"]}}}
        self.nodes = []
        self.records = []
        self.node_bytes = 100
        self.calls = []
        self.fail_core_once = False
        self.fail_hosts_once = False
        self.fail_node_once = False
        self.auto_grant = False
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, code, data):
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def do_GET(self):
                self.handle_request("GET")

            def do_POST(self):
                self.handle_request("POST")

            def do_PUT(self):
                self.handle_request("PUT")

            def do_DELETE(self):
                self.handle_request("DELETE")

            def handle_request(self, method):
                path = urlsplit(self.path).path
                data = None
                if method in ("POST", "PUT"):
                    raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    data = parse_qs(raw.decode()) if path == "/api/admin/token" else json.loads(raw)
                fixture.calls.append((method, path, copy.deepcopy(data)))
                result = {}
                if path == "/api/admin/token":
                    result = {"access_token": "fixture-panel-token"}
                elif path.startswith("/api/") and self.headers.get("Authorization") != "Bearer fixture-panel-token":
                    return self.respond(401, {"detail": "unauthorized"})
                elif path == "/api/node/settings":
                    result = {"certificate": "-----BEGIN CERTIFICATE-----\nfixture\n-----END CERTIFICATE-----"}
                elif path == "/api/core/config":
                    if method == "PUT":
                        old_tags = {i["tag"] for i in fixture.core["inbounds"]}
                        fixture.core = data
                        remaining = {i["tag"] for i in data["inbounds"]}
                        fixture.hosts = {tag: rows for tag, rows in fixture.hosts.items() if tag in remaining}
                        for user in fixture.users.values():
                            for protocol, tags in user["inbounds"].items():
                                user["inbounds"][protocol] = [tag for tag in tags if tag in remaining]
                        if fixture.auto_grant:
                            new = {i["tag"] for i in data["inbounds"]} - old_tags
                            fixture.users["existing"]["inbounds"]["vless"].extend(sorted(new))
                        if fixture.fail_core_once:
                            fixture.fail_core_once = False
                            return self.respond(500, {"detail": "fixture-secret-should-not-appear"})
                    result = fixture.core
                elif path == "/api/hosts":
                    if method == "PUT":
                        fixture.hosts.update(data)
                        if fixture.fail_hosts_once:
                            fixture.fail_hosts_once = False
                            return self.respond(500, {})
                    result = fixture.hosts
                elif path == "/api/nodes":
                    result = fixture.nodes
                elif path == "/api/node" and method == "POST":
                    if data["add_as_new_host"]:
                        return self.respond(400, {})
                    node = dict(data, id=len(fixture.nodes)+1, status="connected")
                    fixture.nodes.append(node)
                    if fixture.fail_node_once:
                        fixture.fail_node_once = False
                        return self.respond(500, {})
                    result = node
                elif path.startswith("/api/node/"):
                    node = next((n for n in fixture.nodes if n["id"] == int(path.split("/")[3])), None)
                    if not node:
                        return self.respond(404, {})
                    if method == "PUT":
                        node.update(data)
                    result = node
                elif path == "/api/nodes/usage":
                    result = {"usages": [{"node_id": n["id"], "uplink": fixture.node_bytes, "downlink": 0} for n in fixture.nodes]}
                elif path == "/api/users":
                    query = parse_qs(urlsplit(self.path).query)
                    offset = int(query.get("offset", [0])[0])
                    result = {"users": list(fixture.users.values())[offset:offset+100]}
                elif path == "/api/user" and method == "POST":
                    result = dict(data, used_traffic=0, proxies={"vless": {"id": str(uuid4()), "flow": ""}})
                    fixture.users[data["username"]] = result
                elif path.startswith("/api/user/"):
                    name = path.rsplit("/", 1)[1]
                    if name not in fixture.users:
                        return self.respond(404, {})
                    result = fixture.users[name]
                    if method == "DELETE":
                        fixture.users.pop(name)
                        result = {}
                elif path == "/zones":
                    result = {"success": True, "result": [{"id": "fixture-zone"}]}
                elif path == "/zones/fixture-zone/dns_records":
                    if method == "POST":
                        row = dict(data, id="fixture-record")
                        fixture.records.append(row)
                        result = {"success": True, "result": row}
                    else:
                        name = parse_qs(urlsplit(self.path).query)["name"][0]
                        result = {"success": True, "result": [r for r in fixture.records if r["name"] == name]}
                elif path.startswith("/zones/fixture-zone/dns_records/") and method == "PUT":
                    fixture.records[0].update(data)
                    result = {"success": True, "result": fixture.records[0]}
                else:
                    return self.respond(404, {})
                self.respond(200, result)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class FakeHost:
    def __init__(self):
        self.events = []

    def dependencies(self): self.events.append("dependencies")
    def prepare(self, cfg, cert, token): self.events.append("prepare")
    def firewall(self, cfg): self.events.append("firewall")
    def start(self, cfg): self.events.append("start")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.http = HTTPFixture()
        self.panel = Panel(self.http.base)
        self.panel.login("fixture-admin", "fixture-admin-password")
        self.cfg = copy.deepcopy(CONFIG)
        self.cfg["panel_url"] = self.http.base
        self.cloud = Cloudflare(self.http.base, "fixture-dns-token")
        self.host = FakeHost()

    def tearDown(self):
        self.http.close()

    def plan(self):
        with patch("installer.generate_keys", return_value=KEYS):
            return prepare_plan(self.panel, self.cfg, occupied=set())

    def test_full_api_flow_preserves_accounts_and_default_grants(self):
        before = copy.deepcopy(self.http.users)
        old_host = copy.deepcopy(self.http.hosts["old"])
        journal = self.plan()
        snapshots = []
        apply(self.panel, self.cloud, self.host, journal, lambda: snapshots.append(copy.deepcopy(journal)))
        self.assertEqual(journal["phase"], "published")
        self.assertEqual(self.http.users, before)
        self.assertEqual(self.http.hosts["old"], old_host)
        self.assertEqual(self.http.nodes[0]["add_as_new_host"], False)
        self.assertEqual(self.host.events, ["dependencies", "prepare", "firewall", "start"])
        self.assertEqual(len(self.http.records), 1)
        self.assertEqual({i["tag"] for i in self.http.core["inbounds"]}, {"old", "us04-reality", "us04-ws"})
        raw = json.dumps(snapshots)
        self.assertNotIn("fixture-admin-password", raw)
        self.assertNotIn("fixture-panel-token", raw)
        self.assertNotIn("fixture-dns-token", raw)

    def test_retry_does_not_duplicate_dns_nodes_or_inbounds(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        puts = sum(c[:2] == ("PUT", "/api/core/config") for c in self.http.calls)
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.assertEqual(len(self.http.nodes), 1)
        self.assertEqual(len(self.http.records), 1)
        self.assertEqual(len(self.http.core["inbounds"]), 3)
        self.assertEqual(sum(c[:2] == ("PUT", "/api/core/config") for c in self.http.calls), puts)

    def test_core_timeout_after_write_is_reconciled(self):
        journal = self.plan()
        self.http.fail_core_once = True
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.assertTrue(journal["core_applied"])

    def test_node_timeout_after_creation_is_reconciled(self):
        journal = self.plan()
        self.http.fail_node_once = True
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.assertEqual(journal["node_api_id"], 1)
        self.assertEqual(len(self.http.nodes), 1)

    def test_host_timeout_after_write_is_reconciled(self):
        journal = self.plan()
        self.http.fail_hosts_once = True
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.assertTrue(journal["hosts_applied"])

    def test_concurrent_config_change_aborts_without_losing_it(self):
        journal = self.plan()
        self.http.core["routing"] = {"domainStrategy": "AsIs", "rules": []}
        with self.assertRaisesRegex(InstallError, "发生变化"):
            publish(self.panel, journal["desired_inbounds"], journal, lambda: None)
        self.assertIn("routing", self.http.core)
        self.assertEqual(len(self.http.core["inbounds"]), 1)

    def test_unknown_existing_tag_is_rejected(self):
        self.http.core["inbounds"].append({"tag": "us04-ws", "port": 19000})
        with self.assertRaisesRegex(InstallError, "标签"):
            self.plan()

    def test_existing_unmanaged_node_is_not_adopted(self):
        self.http.nodes.append({"name": "us04", "address": "8.8.8.8", "id": 1})
        with self.assertRaisesRegex(InstallError, "已有"):
            self.plan()

    def test_reconfigure_rejects_another_nodes_address(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.http.nodes.append({"id": 2, "name": "someone-else", "address": "1.1.1.1"})
        with self.assertRaisesRegex(InstallError, "已有"):
            prepare_plan(self.panel, dict(journal["config"], address="1.1.1.1"), journal)

    def test_reconfigure_keeps_pending_probe_cleanup_record(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        journal.update(probe_username="fixture_pending", probe_note="owned marker")
        next_plan = prepare_plan(self.panel, journal["config"], journal)
        self.assertEqual(next_plan["probe_username"], "fixture_pending")
        self.assertEqual(next_plan["probe_note"], "owned marker")

    def test_dns_wrong_address_is_not_overwritten(self):
        self.http.records.append({"id": "external", "name": self.cfg["domain"], "type": "A", "content": "1.1.1.1", "proxied": True})
        with self.assertRaisesRegex(InstallError, "其他地址"):
            self.cloud.ensure_dns("fixture-zone", self.cfg["domain"], self.cfg["address"])
        self.assertEqual(self.http.records[0]["content"], "1.1.1.1")

    def test_matching_preexisting_dns_is_reused_without_ownership(self):
        self.http.records.append({"id": "external", "name": self.cfg["domain"], "type": "A", "content": self.cfg["address"], "proxied": True})
        self.assertEqual(self.cloud.ensure_dns("fixture-zone", self.cfg["domain"], self.cfg["address"]), ("external", False))

    def test_owned_dns_can_follow_new_ip(self):
        self.cloud.ensure_dns("fixture-zone", self.cfg["domain"], self.cfg["address"])
        self.cloud.ensure_dns("fixture-zone", self.cfg["domain"], "1.1.1.1", "fixture-record")
        self.assertEqual(self.http.records[0]["content"], "1.1.1.1")

    def test_new_user_auto_assignment_is_detected_and_core_withdrawn(self):
        self.http.auto_grant = True
        journal = self.plan()
        with self.assertRaisesRegex(InstallError, "自动授予"):
            apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.assertEqual([i["tag"] for i in self.http.core["inbounds"]], ["old"])
        self.assertEqual(self.http.users["existing"]["inbounds"], {"vless": ["old"]})

    def test_rollback_retains_unrelated_changes_and_counters(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.http.core["routing"] = {"rules": []}
        self.http.users["existing"]["used_traffic"] = 500
        rollback_core(self.panel, journal)
        self.assertEqual(self.http.core["routing"], {"rules": []})
        self.assertEqual(self.http.users["existing"]["used_traffic"], 500)

    def test_rollback_refuses_modified_managed_inbound(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.http.core["inbounds"][-1]["port"] = 19000
        with self.assertRaisesRegex(InstallError, "外部修改"):
            rollback_core(self.panel, journal)

    def test_reconfigure_rollback_restores_hosts_and_is_repeatable(self):
        initial = self.plan()
        apply(self.panel, self.cloud, self.host, initial, lambda: None)
        old_hosts = copy.deepcopy(self.http.hosts)
        changed = dict(initial["config"], name="New name")
        journal = prepare_plan(self.panel, changed, initial)
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        rollback_core(self.panel, journal)
        rollback_core(self.panel, journal)
        self.assertEqual(self.http.hosts, old_hosts)

    def test_rollback_preserves_external_host_edits(self):
        initial = self.plan()
        apply(self.panel, self.cloud, self.host, initial, lambda: None)
        journal = prepare_plan(self.panel, dict(initial["config"], name="New name"), initial)
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        self.http.hosts["us04-ws"][0]["remark"] = "External change"
        with self.assertRaisesRegex(InstallError, "Host"):
            rollback_core(self.panel, journal)
        self.assertEqual(self.http.hosts["us04-ws"][0]["remark"], "External change")

    def test_probe_verifies_each_protocol_and_cleans_account(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        seen = []
        def runner(cfg, keys, ident, kind):
            seen.append(kind)
            row = self.http.users[journal["probe_username"]]
            row["used_traffic"] += 1234
            self.http.node_bytes += 1234
        result = verify(self.panel, journal["config"], KEYS, journal, lambda: None, runner, seconds=1)
        self.assertEqual(seen, ["reality", "ws"])
        self.assertEqual(result["test_account_bytes"], 2468)
        self.assertEqual(result["node_bytes"], 2468)
        self.assertEqual(set(self.http.users), {"existing"})

    def test_failed_probe_cleans_account_and_retains_original(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        def runner(*args): raise InstallError("fixture probe failure")
        with self.assertRaisesRegex(InstallError, "fixture probe failure"):
            verify(self.panel, journal["config"], KEYS, journal, lambda: None, runner, seconds=1)
        self.assertEqual(set(self.http.users), {"existing"})

    def test_metering_failure_is_not_reported_as_success(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        with self.assertRaisesRegex(InstallError, "流量统计"):
            verify(self.panel, journal["config"], KEYS, journal, lambda: None, lambda *a: None, seconds=0)
        self.assertNotIn("verification", journal)
        self.assertEqual(set(self.http.users), {"existing"})

    def test_interactive_plan_is_read_only_and_secrets_stay_hidden(self):
        answers = [self.http.base, "8.8.4.4", "us04", "Fixture", "8.8.8.8", "both", "www.cloudflare.com",
                   "example.test", "us04-ws.example.test", "owner@example.test", "", "", "", "", "fixture_admin"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)/"node"
            output = io.StringIO()
            with patch("builtins.input", side_effect=answers), patch("installer.getpass.getpass", side_effect=["fixture-password", "fixture-dns-token"]), \
                 patch("installer.urlopen", side_effect=OSError), patch("installer.local_ports", return_value=set()), \
                 patch("installer.Cloudflare", side_effect=lambda base, token: Cloudflare(self.http.base, token)), redirect_stdout(output):
                execute(Namespace(root=str(root), command="install", plan=True))
            self.assertFalse(root.exists())
            self.assertEqual(self.http.records, [])
            self.assertEqual(self.http.nodes, [])
            self.assertNotIn("fixture-password", output.getvalue())
            self.assertNotIn("fixture-dns-token", output.getvalue())

    def test_expired_probe_from_interrupted_run_is_replaced(self):
        journal = self.plan()
        apply(self.panel, self.cloud, self.host, journal, lambda: None)
        journal.update(probe_username="fixture_probe", probe_note="owned marker")
        self.http.users["fixture_probe"] = {"username": "fixture_probe", "note": "owned marker", "status": "expired",
            "expire": 1, "used_traffic": 0, "data_limit": 1000}
        def runner(cfg, keys, ident, kind):
            self.http.users["fixture_probe"]["used_traffic"] += 100
            self.http.node_bytes += 100
        result = verify(self.panel, journal["config"], KEYS, journal, lambda: None, runner, seconds=1)
        self.assertEqual(result["test_account_bytes"], 200)
        self.assertNotIn("fixture_probe", self.http.users)


class PureTests(unittest.TestCase):
    def test_plan_flag_cannot_run_mutating_check(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit_code:
            run_cli(["check", "--plan"])
        self.assertEqual(exit_code.exception.code, 2)

    def test_rejects_plain_http_except_loopback(self):
        with self.assertRaises(InstallError): API("http://panel.example.test")
        API("http://127.0.0.1:1234")

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(InstallError): API("https://user:pass@panel.example.test")

    def test_rejects_non_public_and_invalid_ip(self):
        for value in ("127.0.0.1", "10.0.0.1", "203.0.113.1", "1.2.3.999", "8.8.8.8;touch /tmp/pwn"):
            with self.subTest(value=value), self.assertRaises(InstallError): public_ipv4(value)

    def test_domains_and_identifiers_are_not_shell_fragments(self):
        for key, value in (("node_id", "../../evil"), ("domain", "a.example.test;id"), ("domain", "elsewhere.test"),
                           ("email", "a\nb@example.test"), ("sni", "www.example.test:443"), ("name", "node\nnext")):
            cfg = dict(CONFIG, **{key: value})
            with self.subTest(key=key), self.assertRaises(InstallError): validate(cfg)

    def test_global_and_local_port_collisions_are_avoided(self):
        core = {"inbounds": [{"port": "12020-12022"}, {"port": "18020,18021"}]}
        cfg = allocate_ports(CONFIG, core, {12023, 18022})
        self.assertEqual((cfg["reality_port"], cfg["ws_port"]), (12024, 18023))

    def test_duplicate_management_ports_rejected(self):
        with self.assertRaises(InstallError): allocate_ports(dict(CONFIG, api_port=62050), {}, set())

    def test_management_port_cannot_collide_with_global_inbound(self):
        with self.assertRaisesRegex(InstallError, "已有入站"):
            allocate_ports(CONFIG, {"inbounds": [{"port": 62050}]}, set())

    def test_explicit_busy_port_rejected(self):
        with self.assertRaises(InstallError): allocate_ports(dict(CONFIG, reality_port=12025), {}, {12025})

    def test_ws_only_needs_no_reality_listener(self):
        cfg = allocate_ports(dict(CONFIG, mode="ws"), {}, set())
        rows = inbounds(cfg, {})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["listen"], "127.0.0.1")
        self.assertEqual(rows[0]["streamSettings"]["security"], "none")

    def test_reality_only_compose_does_not_include_ingress(self):
        cfg = allocate_ports(dict(CONFIG, mode="reality"), {}, set())
        rendered = compose_config(cfg, "/opt/marzban-exit")
        self.assertEqual(set(rendered["services"]), {"node"})

    def test_secret_not_in_compose_or_nginx(self):
        cfg = allocate_ports(CONFIG, {}, set())
        text = json.dumps(compose_config(cfg, "/opt/marzban-exit")) + nginx_config(cfg)
        self.assertNotIn(KEYS["private"], text)
        self.assertNotIn("cloudflare.ini", text)
        self.assertIn("127.0.0.1:18020", text)

    def test_ws_subscription_and_reality_client_use_matching_parameters(self):
        cfg = allocate_ports(CONFIG, {}, set())
        reality = client_config(cfg, KEYS, str(uuid4()), "reality", 12345)
        ws = client_config(cfg, KEYS, str(uuid4()), "ws", 12346)
        self.assertEqual(reality["outbounds"][0]["streamSettings"]["realitySettings"]["publicKey"], KEYS["public"])
        self.assertNotIn("flow", ws["outbounds"][0]["settings"]["vnext"][0]["users"][0])
        self.assertNotIn("allowInsecure", json.dumps(ws))
        self.assertEqual(hosts(cfg)["us04-ws"][0]["port"], 443)

    def test_private_writes_are_atomic_and_symlinks_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"state.json"
            save_json(path, {"phase": "complete"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            target = Path(directory)/"link"
            target.symlink_to(path)
            with self.assertRaises(InstallError): write_private(target, "bad")
            self.assertEqual(json.loads(path.read_text())["phase"], "complete")

    def test_real_openssl_keys_have_correct_shape(self):
        keys = generate_keys()
        self.assertEqual(len(keys["private"]), 43)
        self.assertEqual(len(keys["public"]), 43)
        self.assertNotEqual(keys["private"], keys["public"])

    def test_release_path_traversal_is_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            item = tarfile.TarInfo("root/../evil")
            item.size = 1
            archive.addfile(item, io.BytesIO(b"x"))
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(InstallError):
            extract_bundle(buf.getvalue(), directory)

    def test_firewall_preserves_ssh_and_scopes_control_and_cloudflare(self):
        calls = []
        def fake_run(args):
            calls.append(args)
            return b"[ 4] 10000/tcp ALLOW IN Anywhere # another-service\n[ 7] 443/tcp DENY IN Anywhere # marzban-node-bootstrap\n" if args[:3] == ["ufw", "status", "numbered"] else b""
        reply = io.BytesIO(b"173.245.48.0/20\n103.21.244.0/22\n")
        cfg = allocate_ports(CONFIG, {}, set())
        with patch("system.run", side_effect=fake_run), patch("system.urlopen", return_value=reply), \
             patch.dict(os.environ, {"SSH_CONNECTION": "1.1.1.1 50000 8.8.8.8 2222"}):
            Host("/opt/marzban-exit").firewall(cfg)
        self.assertIn(["ufw", "--force", "delete", "7"], calls)
        self.assertNotIn(["ufw", "--force", "delete", "4"], calls)
        self.assertIn(["ufw", "allow", "2222/tcp", "comment", "marzban-node-bootstrap"], calls)
        self.assertIn(["ufw", "insert", "1", "allow", "from", CONFIG["panel_ip"], "to", "any", "port", "62050", "proto", "tcp", "comment", "marzban-node-bootstrap"], calls)
        deny = ["ufw", "insert", "1", "deny", "443/tcp", "comment", "marzban-node-bootstrap"]
        allow = ["ufw", "insert", "1", "allow", "from", "173.245.48.0/20", "to", "any", "port", "443", "proto", "tcp", "comment", "marzban-node-bootstrap"]
        self.assertLess(calls.index(deny), calls.index(allow))
        self.assertEqual(calls[-1], ["ufw", "--force", "enable"])


if __name__ == "__main__":
    unittest.main()
