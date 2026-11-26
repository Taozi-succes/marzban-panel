"""Check visible progress without printing command arguments or secret output."""
import io
from pathlib import Path
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/"deploy/node"))
from api import InstallError
from system import Host, run


class ProgressTests(unittest.TestCase):
    def test_long_command_reports_wait_but_keeps_output_private(self):
        output = io.StringIO()
        with patch("system.HEARTBEAT_SECONDS", .01), redirect_stdout(output):
            result = run([sys.executable, "-c", "import time;time.sleep(.08);print('fixture-secret')"])
            finished_output = output.getvalue()
            time.sleep(.03)
            self.assertEqual(output.getvalue(), finished_output)
        self.assertEqual(result, b"fixture-secret\n")
        self.assertIn("[等待]", output.getvalue())
        self.assertNotIn("fixture-secret", output.getvalue())

    def test_timeout_is_safe_and_cleans_up_progress_thread(self):
        output = io.StringIO()
        args = ["fixture-command", "--private-value", "fixture-secret"]
        with patch("system.subprocess.run", side_effect=subprocess.TimeoutExpired(args, 900, output=b"fixture-secret")), redirect_stdout(output):
            with self.assertRaises(InstallError) as result:
                run(args)
        self.assertNotIn("fixture-secret", str(result.exception)+output.getvalue())

    def test_status_before_compose_exists_does_not_invoke_docker(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder, patch("system.run") as command, redirect_stdout(io.StringIO()):
            Host(folder).status()
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
