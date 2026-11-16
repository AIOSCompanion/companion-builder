import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import deploy_bridge


class BridgeDeploymentTests(unittest.TestCase):
    def test_missing_credentials_prevent_any_deployment(self):
        with self.assertRaises(ValueError):
            deploy_bridge.deploy({}, run=lambda *a, **kw: self.fail("deployed without credentials"))

    def test_keys_go_through_stdin_and_bridge_stays_disabled_until_all_keys_are_uploaded(self):
        env = {name: "synthetic-" + name for name in deploy_bridge.REQUIRED}
        env.update(BRIDGE_APP_ID="12345", BRIDGE_ENABLED="true")
        calls = []
        def run(args, **kw):
            calls.append((args, kw))
            return subprocess.CompletedProcess(args, 0)
        deploy_bridge.deploy(env, run)
        self.assertIn("BRIDGE_ENABLED:false", calls[0][0])
        self.assertIn("BRIDGE_ENABLED:true", calls[-1][0])
        for args, kwargs in calls:
            for name in ("BRIDGE_APP_PRIVATE_KEY", "BRIDGE_WEBHOOK_SECRET", "BRIDGE_STATUS_TOKEN"):
                self.assertNotIn(env[name], args)
                self.assertNotIn(name, kwargs["env"])
            self.assertTrue(kwargs["capture_output"])
        self.assertEqual([kw["input"] for _, kw in calls[1:4]], [env[n] for n in (
            "BRIDGE_APP_PRIVATE_KEY", "BRIDGE_WEBHOOK_SECRET", "BRIDGE_STATUS_TOKEN")])

    def test_failed_secret_upload_cannot_enable_the_worker(self):
        env = {name: "synthetic" for name in deploy_bridge.REQUIRED}
        env.update(BRIDGE_APP_ID="12345", BRIDGE_ENABLED="true")
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1 if "secret" in args else 0)
        with self.assertRaises(RuntimeError):
            deploy_bridge.deploy(env, run)
        self.assertFalse(any("BRIDGE_ENABLED:true" in args for args in calls))
