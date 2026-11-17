import base64
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_macos import package


class MacPackageTests(unittest.TestCase):
    def bundle(self, root):
        bundle = Path(root) / "Companion_1.2.3-rc.1_darwin_arm64.app"
        (bundle / "Contents/MacOS").mkdir(parents=True)
        (bundle / "Contents/MacOS/Companion").write_bytes(b"\xcf\xfa\xed\xfe synthetic Mach-O")
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "Companion", "CFBundleShortVersionString": "1.2.3"}))
        return bundle

    def test_explicit_unsigned_mode_never_reads_credentials_or_signs(self):
        class NoSecrets(dict):
            def get(self, *args):
                raise AssertionError("Unsigned packaging read a secret")
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return b"1.2.3-rc.1\n" if args[-1] == "version" else b""
        with tempfile.TemporaryDirectory() as root, \
                patch("app_release_macos.platform.system", return_value="Darwin"), \
                patch("app_release_macos.platform.machine", return_value="arm64"), \
                patch("app_release_macos.run", side_effect=run):
            self.bundle(root)
            package(root, "v1.2.3-rc.1", NoSecrets(), True)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[-1][0], "ditto")
            self.assertTrue(calls[-1][-1].endswith("_unsigned-test.zip"))

    def test_missing_signing_credentials_cannot_silently_create_an_unsigned_archive(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("app_release_macos.platform.system", return_value="Darwin"), \
                patch("app_release_macos.platform.machine", return_value="arm64"), \
                patch("app_release_macos.run", return_value=b"1.2.3-rc.1\n") as run:
            self.bundle(root)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                package(root, "v1.2.3-rc.1", {})
            self.assertEqual(run.call_count, 1)
            self.assertFalse(list(Path(root).glob("*.zip")))

    def test_rejected_notarization_cleans_keychain_and_never_staples_or_creates_final_zip(self):
        calls = []
        secrets = {"CSC_LINK": base64.b64encode(b"synthetic P12").decode(), "CSC_KEY_PASSWORD": "synthetic",
                   "APPLE_API_KEY": "synthetic P8", "APPLE_API_KEY_ID": "ID", "APPLE_API_ISSUER": "ISSUER"}
        def run(args, **kwargs):
            calls.append(args)
            if args[-1] == "version":
                return b"1.2.3-rc.1\n"
            if "find-identity" in args:
                return b'1) ' + b"A" * 40 + b' "Developer ID Application: Synthetic (TEST)"\n'
            if "notarytool" in args:
                return b'{"status":"Invalid"}'
            return b""
        with tempfile.TemporaryDirectory() as root, \
                patch("app_release_macos.platform.system", return_value="Darwin"), \
                patch("app_release_macos.platform.machine", return_value="arm64"), \
                patch("app_release_macos.run", side_effect=run):
            self.bundle(root)
            with self.assertRaisesRegex(ValueError, "did not accept"):
                package(root, "v1.2.3-rc.1", secrets)
            self.assertEqual(calls[-1][:2], ["security", "delete-keychain"])
            keychain_folder = Path(calls[-1][-1]).parent
            self.assertFalse(keychain_folder.exists())
            self.assertFalse(any("stapler" in args for args in calls))
            self.assertFalse(list(Path(root).glob("*.zip")))
