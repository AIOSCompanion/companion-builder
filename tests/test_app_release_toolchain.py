import json
import sys
from pathlib import Path
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_toolchain import go_version, node_satisfies, node_setup_version, node_spec, resolve


class ToolchainTests(unittest.TestCase):
    def test_go_comes_from_the_toolchain_line_else_the_go_line_and_must_be_complete(self):
        self.assertEqual(go_version("module m\n\ngo 1.27.4\n"), "1.27.4")
        self.assertEqual(go_version("module m\n\ngo 1.27\n\ntoolchain go1.27.5\n"), "1.27.5")
        self.assertEqual(go_version("module m\ngo 1.27.4 // pinned\n"), "1.27.4")
        for text in ("module m\n", "module m\ngo 1.27\n", "module m\ngo 1.27.4\ngo 1.27.5\n",
                     "module m\ngo 1.27.4\ntoolchain default\n", "module m\ngo 1.27.4\ntoolchain go1.27\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                go_version(text)

    def test_node_is_the_engines_exact_version_or_the_major_line_of_its_floor(self):
        self.assertEqual(node_setup_version(node_spec('{"engines": {"node": "22.23.4"}}')), "22.23.4")
        floor = node_spec('{"engines": {"node": ">=22.12.0"}}')  # the UI's engines on 17 Nov 2026
        self.assertEqual(node_setup_version(floor), "22")
        self.assertTrue(node_satisfies(floor, "v22.23.4"))
        self.assertTrue(node_satisfies(floor, "v22.12.0\n"))
        self.assertFalse(node_satisfies(floor, "v22.11.9"))
        self.assertFalse(node_satisfies(floor, "garbage"))
        exact = node_spec('{"engines": {"node": "22.23.4"}}')
        self.assertFalse(node_satisfies(exact, "v22.23.5"))
        for text in ('{}', '{"engines": {}}', '{"engines": {"node": "^22.12.0"}}', '{"engines": {"node": "22"}}',
                     '{"engines": {"node": ">=22.12.0 <24"}}', 'not json', '[]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                node_spec(text)

    def test_versions_are_read_from_the_pinned_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "ui").mkdir()
            (Path(root) / "go.mod").write_text("module github.com/AIOSCompanion/companion-engine\n\ngo 1.27.4\n")
            (Path(root) / "ui/package.json").write_text(json.dumps({"engines": {"node": ">=22.12.0"}}))
            self.assertEqual(resolve(root), {"go_version": "1.27.4", "node_version": "22"})
            (Path(root) / "ui/package.json").unlink()
            with self.assertRaisesRegex(ValueError, "missing"):
                resolve(root)


if __name__ == "__main__":
    unittest.main()
