import base64
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import app_release_macos
from app_release_macos import package, signing_inputs, staple, verify_signature, verify_signed_archive

UUID = "2e3f4a5b-6c7d-4e8f-9a0b-1c2d3e4f5a6b"
GOOD_SIGNATURE = ("Executable=/x/Companion.app/Contents/MacOS/Companion\n"
                  "CodeDirectory v=20500 size=1 flags=0x10000(runtime) hashes=1+7 location=embedded\n"
                  "Authority=Developer ID Application: Synthetic Team (ABCDE12345)\n"
                  "Authority=Developer ID Certification Authority\nAuthority=Apple Root CA\n"
                  "Timestamp=17 Nov 2026 at 20:00:00\nTeamIdentifier=ABCDE12345\n")


class FakeTools:
    """Records every tool call; answers like the macOS tools would."""

    def __init__(self, notary='{"id": "%s", "status": "Accepted"}' % UUID, staple_failures=0,
                 signature=GOOD_SIGNATURE, assessment=(0, "accepted\nsource=Notarized Developer ID\n")):
        self.calls, self.notary, self.staple_failures = [], notary, staple_failures
        self.signature, self.assessment = signature, assessment

    def __call__(self, args, timeout=180, env=None):
        self.calls.append(list(args))
        if args[-1] == "version":
            return 0, "1.2.3-rc.1\n", "1.2.3-rc.1\n"
        if args[:2] == ["security", "list-keychains"] and len(args) == 4:
            return 0, '    "/Users/runner/Library/Keychains/login.keychain-db"\n', '    "/Users/runner/Library/Keychains/login.keychain-db"\n'
        if "find-identity" in args:
            line = '  1) ' + "A" * 40 + ' "Developer ID Application: Synthetic Team (ABCDE12345)"\n'
            return 0, line, line
        if args[:3] == ["xcrun", "notarytool", "submit"]:
            return 0, self.notary, self.notary
        if args[:3] == ["xcrun", "stapler", "staple"]:
            if self.staple_failures:
                self.staple_failures -= 1
                return 65, "CloudKit query failed\n", ""
        if args[:2] == ["codesign", "-d"]:
            return 0, self.signature, ""
        if args[0] == "spctl":
            return self.assessment[0], self.assessment[1], ""
        if args[:3] == ["ditto", "-x", "-k"]:
            app = Path(args[-1]) / "Companion.app"
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/MacOS/Companion").write_bytes(b"\xcf\xfa\xed\xfe synthetic")
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleExecutable": "Companion", "CFBundleShortVersionString": "1.2.3"}))
        return 0, "", ""

    def named(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]


class MacPackageTests(unittest.TestCase):
    SECRETS = {"CSC_LINK": base64.b64encode(b"synthetic P12").decode(), "CSC_KEY_PASSWORD": "synthetic-password",
               "APPLE_API_KEY": "-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----",
               "APPLE_API_KEY_ID": "ABCDE12345", "APPLE_API_ISSUER": "00000000-0000-0000-0000-000000000001"}

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        bundle = self.root / "Companion_1.2.3-rc.1_darwin_arm64.app"
        (bundle / "Contents/MacOS").mkdir(parents=True)
        (bundle / "Contents/MacOS/Companion").write_bytes(b"\xcf\xfa\xed\xfe synthetic Mach-O")
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "Companion", "CFBundleShortVersionString": "1.2.3"}))
        for name, value in (("system", "Darwin"), ("machine", "arm64")):
            patcher = patch(f"app_release_macos.platform.{name}", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def package(self, tools, env, unsigned=False):
        with patch("app_release_macos.attempt", side_effect=tools):
            package(self.root, "v1.2.3-rc.1", env, unsigned, sleep=lambda _: None)

    def test_explicit_unsigned_mode_never_reads_credentials_or_signs(self):
        class NoSecrets(dict):
            def get(self, key, *args):
                if key in app_release_macos.SECRET_NAMES or key.startswith("APPLE_"):
                    raise AssertionError("Unsigned packaging read a secret")
                return super().get(key, *args)
        tools = FakeTools()
        self.package(tools, NoSecrets(), unsigned=True)
        self.assertEqual([call[0] for call in tools.calls], [str(self.root / "Companion_1.2.3-rc.1_darwin_arm64.app"
                                                                 / "Contents/MacOS/Companion"), "ditto", "ditto"])
        self.assertTrue(tools.calls[-1][-1].endswith("_unsigned-test.zip"))
        self.assertTrue(tools.calls[-1][-2].endswith("Companion.app"))  # the download unpacks to the app's name

    def test_missing_signing_credentials_cannot_silently_create_an_unsigned_archive(self):
        tools = FakeTools()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.package(tools, {})
        self.assertFalse(tools.named("security"))
        self.assertFalse(list(self.root.glob("*.zip")))

    def test_rejected_notarization_fetches_apples_log_cleans_up_and_never_staples(self):
        tools = FakeTools(notary='{"id": "%s", "status": "Invalid"}' % UUID)
        with self.assertRaisesRegex(ValueError, "did not accept.*Invalid"):
            self.package(tools, dict(self.SECRETS))
        self.assertEqual(len(tools.named("xcrun", "notarytool", "log", UUID)), 1)
        self.assertFalse(tools.named("xcrun", "stapler"))
        self.assertEqual(len(tools.named("security", "delete-keychain")), 1)
        restored = tools.named("security", "list-keychains", "-d", "user", "-s")[-1]
        self.assertEqual(restored[5:], ["/Users/runner/Library/Keychains/login.keychain-db"])
        self.assertFalse(list(self.root.glob("*.zip")))
        # Only `security import` receives the certificate password; the raw certificate and key never appear.
        flat = [" ".join(call) for call in tools.calls]
        self.assertEqual(sum(self.SECRETS["CSC_KEY_PASSWORD"] in call for call in flat), 1)
        self.assertFalse(any(self.SECRETS["CSC_LINK"] in call or "BEGIN PRIVATE KEY" in call for call in flat))

    def test_signing_imports_the_pinned_intermediates_and_signs_a_copy_named_companion_app(self):
        tools = FakeTools()
        self.package(tools, dict(self.SECRETS))
        imported = [Path(call[2]).name for call in tools.named("security", "import")]
        self.assertEqual(imported, ["DeveloperIDCA.cer", "DeveloperIDG2CA.cer", "certificate.p12"])
        signed = [call[-1] for call in tools.named("codesign", "--force")]
        self.assertTrue(signed[-1].endswith("Companion.app"))
        self.assertTrue(all("--timestamp" in call and "runtime" in call for call in tools.named("codesign", "--force")))
        self.assertEqual(len(tools.named("xcrun", "stapler", "staple")), 1)
        self.assertTrue(tools.calls[-1][-1].endswith("Companion_1.2.3-rc.1_darwin_arm64.zip"))
        self.assertEqual(len(tools.named("security", "delete-keychain")), 1)

    def test_the_signing_step_runs_only_apples_tools_and_none_receives_a_signing_secret(self):
        # Merge gate F1 (17 Nov 2026): the step holding the five Apple secrets ran the private bundle's
        # `Companion version`, and every tool received APPLE_API_KEY_ID and APPLE_API_ISSUER.
        import os
        import subprocess
        names = {"CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"}
        self.assertEqual(set(self.SECRETS), names)
        recorded = []
        def run(args, **kwargs):
            recorded.append((list(args), kwargs.get("env")))
            out, err = b"", b""
            if args[:2] == ["security", "list-keychains"] and len(args) == 4:
                out = b'    "/Users/runner/Library/Keychains/login.keychain-db"\n'
            elif "find-identity" in args:
                out = ('  1) ' + "A" * 40 + ' "Developer ID Application: Synthetic Team (ABCDE12345)"\n').encode()
            elif args[:3] == ["xcrun", "notarytool", "submit"]:
                out = ('{"id": "%s", "status": "Accepted"}' % UUID).encode()
            elif args[:2] == ["codesign", "-d"]:
                err = GOOD_SIGNATURE.encode()
            elif args[0] == "spctl":
                err = b"accepted\nsource=Notarized Developer ID\n"
            elif args[-1] == "version":
                out = b"1.2.3-rc.1\n"
            return subprocess.CompletedProcess(args, 0, out, err)
        with patch.dict(os.environ, self.SECRETS), patch("app_release_macos.subprocess.run", side_effect=run):
            package(self.root, "v1.2.3-rc.1", dict(os.environ), sleep=lambda _: None)
        self.assertGreater(len(recorded), 10)
        for args, env in recorded:
            with self.subTest(tool=args[:2]):
                self.assertIn(args[0], {"security", "codesign", "ditto", "xcrun", "spctl"})
                self.assertIsNotNone(env)
                self.assertFalse(names & set(env))

    def test_the_bundled_apple_intermediates_match_their_pins_and_a_mismatch_refuses_before_any_keychain(self):
        import hashlib
        for name, expected in app_release_macos.INTERMEDIATES.items():
            self.assertEqual(hashlib.sha256((app_release_macos.CERTIFICATES / name).read_bytes()).hexdigest(), expected)
        tools = FakeTools()
        with patch.dict(app_release_macos.INTERMEDIATES, {"DeveloperIDG2CA.cer": "0" * 64}):
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.package(tools, dict(self.SECRETS))
        self.assertFalse(tools.named("security"))

    def test_a_line_wrapped_base64_certificate_is_accepted(self):
        encoded = base64.encodebytes(b"synthetic P12 " * 20).decode()  # wrapped at 76 characters
        self.assertIn("\n", encoded)
        certificate, *_ = signing_inputs(dict(self.SECRETS, CSC_LINK=encoded))
        self.assertEqual(certificate, b"synthetic P12 " * 20)
        with self.assertRaisesRegex(ValueError, "base64"):
            signing_inputs(dict(self.SECRETS, CSC_LINK="not base64!"))

    def test_stapling_is_retried_while_apple_propagates_the_ticket_then_fails_closed(self):
        tools = FakeTools(staple_failures=2)
        with patch("app_release_macos.attempt", side_effect=tools):
            staple(Path("Companion.app"), {}, sleep=lambda _: None)
        self.assertEqual(len(tools.named("xcrun", "stapler", "staple")), 3)
        tools = FakeTools(staple_failures=99)
        with patch("app_release_macos.attempt", side_effect=tools), self.assertRaisesRegex(RuntimeError, "stapled"):
            staple(Path("Companion.app"), {}, sleep=lambda _: None)

    def test_only_a_developer_id_hardened_timestamped_notarised_app_passes_verification(self):
        refusals = {
            "ad hoc": FakeTools(signature="Signature=adhoc\nflags=0x20002(adhoc,linker-signed)\n"),
            "no runtime": FakeTools(signature=GOOD_SIGNATURE.replace("0x10000(runtime)", "0x0(none)")),
            "no timestamp": FakeTools(signature=GOOD_SIGNATURE.replace("Timestamp=", "Signed Time=")),
            "no team": FakeTools(signature=GOOD_SIGNATURE.replace("TeamIdentifier=ABCDE12345", "TeamIdentifier=not set")),
            "gatekeeper": FakeTools(assessment=(3, "rejected\nsource=Unnotarized Developer ID\n")),
        }
        for case, tools in refusals.items():
            with self.subTest(case=case), patch("app_release_macos.attempt", side_effect=tools):
                with self.assertRaises(ValueError):
                    verify_signature(Path("Companion.app"))
        for assessment in [(0, "accepted\nsource=Notarized Developer ID\n"), (0, "accepted\noverride=security disabled\n")]:
            tools = FakeTools(assessment=assessment)
            with patch("app_release_macos.attempt", side_effect=tools):
                verify_signature(Path("Companion.app"))
            self.assertEqual(len(tools.named("xcrun", "stapler", "validate")), 1)

    def test_the_signed_archive_is_verified_again_from_its_own_bytes(self):
        archive = self.root / "Companion_1.2.3-rc.1_darwin_arm64.zip"
        archive.write_bytes(b"synthetic zip")
        tools = FakeTools()
        with patch("app_release_macos.attempt", side_effect=tools), \
                patch("app_release_resources.check_macos_runtimes") as runtimes:
            verify_signed_archive(self.root, "v1.2.3-rc.1")
        runtimes.assert_called_once()
        self.assertEqual(len(tools.named("codesign", "--verify")), 1)
        unsigned = FakeTools(signature="Signature=adhoc\n")
        with patch("app_release_macos.attempt", side_effect=unsigned), self.assertRaises(ValueError):
            verify_signed_archive(self.root, "v1.2.3-rc.1")


if __name__ == "__main__":
    unittest.main()
