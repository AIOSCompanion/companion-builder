"""The trusted-script snapshot: a copy outside the workspace, hashed, and verified before use.

If a PR's code can change the scripts that seal the logs, the seal is worthless. These tests pin each way
the snapshot can be changed (edited, added, removed), and the refusals that keep it outside the workspace.
"""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("snapshot_trusted", ROOT / "scripts" / "snapshot_trusted.py")
snapshot_trusted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot_trusted)


def make_writable(root):
    for path in [root, *root.rglob("*")]:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if path.is_dir() else 0))
        except OSError:
            pass


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="snapshot ")
        self.addCleanup(self.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "scripts"
        self.source.mkdir()
        (self.source / "run_sealed.py").write_text("print('wrap')\n", encoding="utf-8")
        (self.source / "summarize_junit.py").write_text("def summarise(p): return 0\n", encoding="utf-8")
        self.dest = self.root / "trusted"
        self.env_saved = os.environ.pop("GITHUB_WORKSPACE", None)
        self.calls = []

    def cleanup(self):
        if self.env_saved is not None:
            os.environ["GITHUB_WORKSPACE"] = self.env_saved
        if self.dest.exists():
            make_writable(self.dest)
        self.temp.cleanup()

    def sudo(self, command):
        self.calls.append(command)

    def test_the_snapshot_records_a_hash_for_every_file_and_asks_for_root_ownership(self):
        count = snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)
        self.assertEqual(count, 2)
        manifest = (self.dest / snapshot_trusted.MANIFEST).read_text(encoding="utf-8")
        self.assertIn("run_sealed.py", manifest)
        self.assertEqual(self.calls, [["sudo", "-n", "chown", "-R", "root:root", str(self.dest)]])

    def test_an_unchanged_snapshot_verifies(self):
        snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)
        self.assertEqual(snapshot_trusted.verify(self.dest), 2)

    def test_an_edited_trusted_file_fails_verification(self):
        snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)
        make_writable(self.dest)
        (self.dest / "run_sealed.py").write_text("print('tampered')\n", encoding="utf-8")
        with self.assertRaisesRegex(snapshot_trusted.SnapshotError, "1 file\\(s\\) differ"):
            snapshot_trusted.verify(self.dest)

    def test_an_added_file_fails_verification(self):
        snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)
        make_writable(self.dest)
        (self.dest / "extra.py").write_text("x = 1\n", encoding="utf-8")
        with self.assertRaisesRegex(snapshot_trusted.SnapshotError, "added or removed"):
            snapshot_trusted.verify(self.dest)

    def test_a_removed_file_fails_verification(self):
        snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)
        make_writable(self.dest)
        (self.dest / "summarize_junit.py").unlink()
        with self.assertRaisesRegex(snapshot_trusted.SnapshotError, "added or removed"):
            snapshot_trusted.verify(self.dest)

    def test_a_snapshot_inside_the_workspace_is_refused(self):
        os.environ["GITHUB_WORKSPACE"] = str(self.root)
        with self.assertRaisesRegex(snapshot_trusted.SnapshotError, "outside the workspace"):
            snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)

    def test_an_existing_snapshot_is_never_reused(self):
        self.dest.mkdir()
        with self.assertRaisesRegex(snapshot_trusted.SnapshotError, "already exists"):
            snapshot_trusted.snapshot(self.source, self.dest, sudo=self.sudo)


if __name__ == "__main__":
    unittest.main()
