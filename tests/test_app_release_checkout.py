import base64
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_checkout import checkout


class AppCheckoutTests(unittest.TestCase):
    def test_token_is_only_in_fetch_environment_and_is_never_persisted_or_passed_as_an_argument(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            calls = []
            token = "synthetic-read-only-credential"
            env = dict(GITHUB_WORKSPACE=str(root), APP_SOURCE_DIR=str(source), GH_TOKEN=token, SOURCE_SHA="a" * 40)
            def run(args, **kwargs):
                calls.append((args, kwargs))
                self.assertNotIn(token, " ".join(args))
                self.assertNotIn("GH_TOKEN", kwargs["env"])
                if "fetch" in args:
                    header = kwargs["env"]["GIT_CONFIG_VALUE_0"]
                    self.assertEqual(header, "AUTHORIZATION: basic " + base64.b64encode(("x-access-token:" + token).encode()).decode())
                else:
                    self.assertNotIn("GIT_CONFIG_VALUE_0", kwargs["env"])
                if "init" in args:
                    (source / ".git").mkdir(parents=True)
                    (source / ".git/config").write_text("[remote origin]\nurl=https://github.com/AIOSCompanion/companion-engine.git\n")
                return subprocess.CompletedProcess(args, 0)
            checkout(env, run)
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[-1][0][-1], "a" * 40)

    def test_existing_or_outside_source_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source").mkdir()
            for source in (root / "source", root / ".." / "outside"):
                with self.assertRaises(ValueError):
                    checkout(dict(GITHUB_WORKSPACE=str(root), APP_SOURCE_DIR=str(source), GH_TOKEN="synthetic", SOURCE_SHA="a" * 40),
                             lambda *a, **k: self.fail("invalid checkout ran git"))
