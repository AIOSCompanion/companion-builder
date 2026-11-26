import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_artifacts import WINDOWS_RESOURCE_FILES, digest, filenames, inspect, prepare, verify_windows_outputs, windows_binaries


class AppArtifactsTests(unittest.TestCase):
    def test_windows_gui_and_cli_are_distinct_even_on_case_insensitive_filesystems(self):
        names = windows_binaries("v1.2.3")
        self.assertEqual(len({name.casefold() for name in names}), 2,
                         "The CLI must not overwrite the GUI on Windows")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / names[0]).write_bytes(b"GUI sentinel")
            (root / names[1]).write_bytes(b"CLI sentinel")
            self.assertEqual((root / names[0]).read_bytes(), b"GUI sentinel")
            self.assertEqual((root / names[1]).read_bytes(), b"CLI sentinel")

    def windows(self, root):
        names = windows_binaries("v1.2.3")
        for name in names:
            (root / name).write_bytes(b"synthetic executable " + name.encode())
        (root / "SHA256SUMS").write_text("".join(f"{digest(root / name)}  {name}\n" for name in names))
        for name in WINDOWS_RESOURCE_FILES:
            resource = root / name
            resource.parent.mkdir(parents=True, exist_ok=True)
            resource.write_bytes(b"synthetic resource")
        return names

    def test_only_portable_package_and_platform_qualified_receipts_are_staged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = self.windows(root)
            (root / "private-source.py").write_text("must never publish")
            output = root / "assets"
            prepare(root, output, "v1.2.3", "a" * 40, "windows-amd64")
            receipt, files = inspect(output, "v1.2.3", "a" * 40, "windows-amd64")
            archive, = filenames("v1.2.3", "windows-amd64")
            self.assertEqual({p.name for p in files}, {archive, "SHA256SUMS-windows-amd64.txt",
                "companion-release-windows-amd64.json", "companion-release-g2-windows-amd64.json"})
            self.assertEqual(receipt["signing"], "unsigned-windows")
            with zipfile.ZipFile(output / archive) as bundle:
                self.assertEqual(set(bundle.namelist()), set(names) | WINDOWS_RESOURCE_FILES)

    def test_missing_or_unexpected_resource_refuses_windows_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.windows(root)
            (root / "resources/private-source.py").write_text("must never publish")
            with self.assertRaisesRegex(ValueError, "unexpected resource"):
                prepare(root, root / "assets", "v1.2.3", "a" * 40, "windows-amd64")
            (root / "resources/private-source.py").unlink()
            (root / next(iter(WINDOWS_RESOURCE_FILES))).unlink()
            with self.assertRaisesRegex(ValueError, "missing a required"):
                prepare(root, root / "second-assets", "v1.2.3", "a" * 40, "windows-amd64")

    def test_tampering_after_staging_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.windows(root)
            output = root / "assets"
            prepare(root, output, "v1.2.3", "a" * 40, "windows-amd64")
            (output / filenames("v1.2.3", "windows-amd64")[0]).write_bytes(b"replaced")
            with self.assertRaisesRegex(ValueError, "hash changed"):
                inspect(output, "v1.2.3", "a" * 40, "windows-amd64")

    def test_missing_checksum_extra_source_entry_and_truncated_binary_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = self.windows(root)
            (root / "SHA256SUMS").write_text(f"{'0' * 64}  private-source.py\n")
            with self.assertRaises(ValueError):
                verify_windows_outputs(root, "v1.2.3")
            self.windows(root)
            (root / names[0]).write_bytes(b"")
            with self.assertRaises(ValueError):
                verify_windows_outputs(root, "v1.2.3")

    def test_unexpected_staged_file_cannot_be_uploaded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.windows(root)
            output = root / "assets"
            prepare(root, output, "v1.2.3", "a" * 40, "windows-amd64")
            (output / "private.log").write_text("synthetic private output")
            with self.assertRaisesRegex(ValueError, "Unexpected"):
                inspect(output, "v1.2.3", "a" * 40, "windows-amd64")

    def test_unsigned_macos_uses_a_distinct_filename_and_truthful_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            name = filenames("v1.2.3", "darwin-arm64", True)[0]
            self.assertIn("unsigned-test", name)
            (root / name).write_bytes(b"synthetic unsigned ZIP")
            prepare(root, root / "assets", "v1.2.3", "a" * 40, "darwin-arm64", True)
            receipt, _ = inspect(root / "assets", "v1.2.3", "a" * 40, "darwin-arm64", True)
            self.assertEqual(receipt["signing"], "unsigned-test")
            with self.assertRaises(ValueError):
                inspect(root / "assets", "v1.2.3", "a" * 40, "darwin-arm64", False)
