import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import compose_guard as cg


class ComposeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.compose_file = self.root / "compose.yml"
        self.compose_file.write_text("services: {}\n")
        self.state = cg.state_path(self.compose_file)
        self.args = argparse.Namespace(service="web", image="example/web:new", watch=1,
                                       interval=1, fails=3, check=None,
                                       notify_telegram=False, notify_webhook=None)
        self.old = [{"Name": "/web-1", "RestartCount": 0,
                     "Config": {"Image": "example/web:old"}}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_successful_deployment_saves_result(self):
        with mock.patch.object(cg, "containers", return_value=self.old), \
             mock.patch.object(cg, "compose"), \
             mock.patch.object(cg, "inspect_ok", return_value=(True, "healthy")), \
             mock.patch.object(cg.time, "sleep"):
            self.assertEqual(cg.run_deploy(self.args, self.compose_file, self.state), 0)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["result"], "success")
        self.assertEqual(state["previous_image"], "example/web:old")

    def test_failure_rolls_back(self):
        with mock.patch.object(cg, "containers", return_value=self.old), \
             mock.patch.object(cg, "compose") as compose, \
             mock.patch.object(cg, "inspect_ok", side_effect=[(False, "down")] * 3 + [(True, "healthy")]), \
             mock.patch.object(cg, "notify"), \
             mock.patch.object(cg.time, "sleep"):
            self.assertEqual(cg.run_deploy(self.args, self.compose_file, self.state), 1)
        self.assertEqual(json.loads(self.state.read_text())["result"], "rolled back")
        self.assertEqual(compose.call_count, 2)

    def test_unreachable_url_fails_check(self):
        with mock.patch.object(cg, "containers", return_value=[{"Name": "/web", "RestartCount": 0,
                                                                  "State": {"Status": "running"}}]), \
             mock.patch.object(cg.urllib.request, "urlopen", side_effect=OSError("offline")):
            ok, reason = cg.inspect_ok("web", self.compose_file, None, {}, "https://example.invalid")
        self.assertFalse(ok)
        self.assertIn("URL check failed", reason)

    def test_live_lock_refuses_second_run(self):
        lock = open(str(self.state) + ".lock", "a+")
        cg.fcntl.flock(lock, cg.fcntl.LOCK_EX | cg.fcntl.LOCK_NB)
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                cg.run_deploy(self.args, self.compose_file, self.state)
        finally:
            lock.close()

    def test_telegram_token_is_not_printed(self):
        self.args.notify_telegram = True
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"CG_TELEGRAM_TOKEN": "secret-token", "CG_TELEGRAM_CHAT": "123"}), \
             mock.patch.object(cg, "containers", side_effect=RuntimeError("docker unavailable")), \
             contextlib.redirect_stderr(output):
            self.assertEqual(cg.main(["deploy", "--service", "web", "--image", "example/web:new",
                                      "--notify-telegram"]), 2)
        self.assertNotIn("secret-token", output.getvalue())


if __name__ == "__main__":
    unittest.main()
