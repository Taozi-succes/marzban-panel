"""Regression for a VPS receiving HTTP 403 from the Cloudflare website."""
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"deploy/node"))
from api import InstallError
from system import Host

CONFIG = {"mode": "both", "panel_ip": "8.8.4.4", "control_port": 62050, "api_port": 62051, "reality_port": 12022}


class CloudflareRangesTests(unittest.TestCase):
    def test_public_api_works_when_website_returns_403(self):
        seen = []
        def fetch(request, **kwargs):
            url = request if isinstance(request, str) else request.full_url
            if "www.cloudflare.com" in url:
                raise HTTPError(url, 403, "Forbidden", {}, None)
            self.assertEqual(url, "https://api.cloudflare.com/client/v4/ips")
            self.assertIsNone(request.get_header("Authorization"))
            self.assertTrue(request.get_header("User-agent"))
            seen.append(url)
            return io.BytesIO(json.dumps({"success": True, "result": {"ipv4_cidrs": ["173.245.48.0/20"]}}).encode())
        with patch("system.urlopen", side_effect=fetch), patch("system.run", return_value=b"") as run, \
             patch.dict(os.environ, {"SSH_CONNECTION": "1.1.1.1 50000 8.8.8.8 22"}):
            Host("/opt/marzban-exit").firewall(CONFIG)
        self.assertEqual(seen, ["https://api.cloudflare.com/client/v4/ips"])
        self.assertTrue(any("173.245.48.0/20" in call.args[0] for call in run.call_args_list))

    def test_http_error_is_clear_and_changes_no_firewall_rule(self):
        error = HTTPError("https://api.cloudflare.com/client/v4/ips", 403, "Forbidden", {}, None)
        with patch("system.urlopen", side_effect=error), patch("system.run") as run:
            with self.assertRaisesRegex(InstallError, "Cloudflare.*HTTP 403"):
                Host("/opt/marzban-exit").firewall(CONFIG)
        run.assert_not_called()

    def test_network_error_does_not_expose_response_or_change_rules(self):
        with patch("system.urlopen", side_effect=URLError("fixture-secret")), patch("system.run") as run:
            with self.assertRaises(InstallError) as result:
                Host("/opt/marzban-exit").firewall(CONFIG)
        self.assertNotIn("fixture-secret", str(result.exception))
        run.assert_not_called()

    def test_invalid_networks_fail_before_any_firewall_change(self):
        for networks in ([], ["0.0.0.0/0"], ["10.0.0.0/8"], ["::/0"], ["invalid"], "173.245.48.0/20"):
            payload = {"success": True, "result": {"ipv4_cidrs": networks}}
            with self.subTest(networks=networks), patch("system.urlopen", return_value=io.BytesIO(json.dumps(payload).encode())), patch("system.run") as run:
                with self.assertRaises(InstallError):
                    Host("/opt/marzban-exit").firewall(CONFIG)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
