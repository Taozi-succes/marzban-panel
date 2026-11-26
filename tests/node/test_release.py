"""Test public bootstrap and anonymous upgrade without networking."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"deploy/node"))
from api import InstallError
from release import FILES, download, extract_bundle, upgrade
from plan import VERSION


def bundle(overrides=None, refresh=False):
    contents = {name: (ROOT/"deploy/node"/name).read_bytes() for name in FILES}
    contents.update(overrides or {})
    if refresh:
        contents["SHA256SUMS"] = "".join(hashlib.sha256(contents[n]).hexdigest()+"  "+n+"\n"
            for n in sorted(FILES - {"SHA256SUMS"})).encode()
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in contents.items():
            entry = tarfile.TarInfo("repository/deploy/node/"+name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


class ReleaseTests(unittest.TestCase):
    def test_actual_versioned_bundle_validates(self):
        with tempfile.TemporaryDirectory() as folder:
            extract_bundle(bundle(), folder)
            self.assertEqual({p.name for p in Path(folder).iterdir()}, FILES)

    def test_modified_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaisesRegex(InstallError, "校验失败"):
            extract_bundle(bundle({"installer.py": b"print('modified')"}), folder)

    def test_malformed_and_duplicate_manifests_are_rejected(self):
        original = (ROOT/"deploy/node/SHA256SUMS").read_bytes()
        for manifest in (b"malformed\n", original+original.splitlines()[0]+b"\n"):
            with self.subTest(manifest=manifest[:10]), tempfile.TemporaryDirectory() as folder, self.assertRaises(InstallError):
                extract_bundle(bundle({"SHA256SUMS": manifest}), folder)

    def bootstrap(self, mode="ok", args=""):
        command = (ROOT/"README.md").read_text().split("```bash\n", 1)[1].split("\n```", 1)[0]+args
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder)
            fake = stage/"bin"
            fake.mkdir()
            contents = bundle({"install.sh": b'#!/bin/bash\necho bootstrap-fixture-installer-started "$@"\n'}, refresh=True)
            if mode == "tampered":
                replacement = io.BytesIO()
                with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as old, tarfile.open(fileobj=replacement, mode="w:gz") as new:
                    for entry in old:
                        data = old.extractfile(entry).read()
                        if entry.name.endswith("/installer.py"):
                            data += b"# corrupted\n"
                        entry.size = len(data)
                        new.addfile(entry, io.BytesIO(data))
                contents = replacement.getvalue()
            (stage/"fixture.tgz").write_bytes(contents)
            tool = fake/"curl"
            tool.write_text('''#!/usr/bin/env python3
import json,os,pathlib,shutil,sys
args=sys.argv[1:]
log=pathlib.Path(os.environ["FIXTURE_CALLS"])
calls=json.loads(log.read_text()) if log.exists() else []
calls.append(args);log.write_text(json.dumps(calls))
if any(a.endswith("/bootstrap.sh") for a in args):
    sys.stdout.write(pathlib.Path(os.environ["FIXTURE_BOOTSTRAP"]).read_text())
elif os.environ["FIXTURE_MODE"]=="download-error":
    sys.exit(22)
else:
    shutil.copyfile(os.environ["FIXTURE_ARCHIVE"],args[args.index("--output")+1])
''')
            tool.chmod(0o700)
            env = dict(os.environ, PATH=str(fake)+":"+os.environ["PATH"], TMPDIR=str(stage),
                FIXTURE_CALLS=str(stage/"calls.json"), FIXTURE_BOOTSTRAP=str(ROOT/"bootstrap.sh"),
                FIXTURE_ARCHIVE=str(stage/"fixture.tgz"), FIXTURE_MODE=mode, GITHUB_TOKEN="unused-secret")
            result = subprocess.run(["bash", "-c", command], input=b"", capture_output=True, env=env, timeout=20)
            if mode == "ok":
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertIn(b"bootstrap-fixture-installer-started", result.stdout)
                if args:
                    self.assertIn(args.strip().encode(), result.stdout)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"bootstrap-fixture-installer-started", result.stdout)
            calls = json.loads((stage/"calls.json").read_text())
            self.assertEqual(len(calls), 2)
            self.assertIn(f"https://raw.githubusercontent.com/Taozi-succes/marzban-node-installer/v{VERSION}/bootstrap.sh", calls[0])
            self.assertIn(f"https://codeload.github.com/Taozi-succes/marzban-node-installer/tar.gz/refs/tags/v{VERSION}", calls[1])
            self.assertNotIn("Authorization", json.dumps(calls))
            self.assertNotIn("unused-secret", json.dumps(calls))
            self.assertNotIn(b"unused-secret", result.stdout+result.stderr)
            self.assertEqual({p.name for p in stage.iterdir()}, {"bin", "calls.json", "fixture.tgz"})

    def test_documented_one_command_is_anonymous_and_cleans_up(self):
        self.bootstrap()

    def test_bootstrap_preserves_plan_argument(self):
        self.bootstrap(args=" --plan")

    def test_bootstrap_rejects_tampered_bundle_before_running(self):
        self.bootstrap(mode="tampered")

    def test_bootstrap_download_failure_does_not_start_installer(self):
        self.bootstrap(mode="download-error")

    def test_upgrade_download_does_not_ask_for_or_send_token(self):
        seen = []
        class Opener:
            def open(self, request, **kwargs):
                seen.append(request)
                return io.BytesIO(b"fixture-archive")
        with patch("release.build_opener", return_value=Opener()), patch("getpass.getpass", side_effect=AssertionError("Token prompt forbidden")):
            self.assertEqual(download("v1.0.1"), b"fixture-archive")
        self.assertEqual(seen[0].full_url, "https://codeload.github.com/Taozi-succes/marzban-node-installer/tar.gz/refs/tags/v1.0.1")
        self.assertIsNone(seen[0].get_header("Authorization"))

    def test_upgrade_uses_verified_bundle_and_existing_root(self):
        with patch("release.download", return_value=bundle()), patch("release.subprocess.run") as run:
            run.return_value.returncode = 0
            upgrade("v1.0.1", Path("/opt/marzban-exit"))
            self.assertEqual(run.call_args.args[0][-3:], ["--root", "/opt/marzban-exit", "reconfigure"])


if __name__ == "__main__":
    unittest.main()
