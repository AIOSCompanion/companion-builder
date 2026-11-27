import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import app_release_resources as resources
from app_release_checkout import RESOURCE_SHA


class CompanionArcStagingTests(unittest.TestCase):
    """stage_resources runs the real staging order with the downloads and external programs replaced by fakes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "resource-source").mkdir(parents=True)
        self.source = self.root / "engine"
        self.source.mkdir()
        self.env = {"GITHUB_WORKSPACE": str(self.workspace), "PATH": "/usr/bin"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_stage(self, platform, stage_writes_arc=True):
        calls = []

        def fake_checked(args, cwd=None, env=None, capture=False, timeout=1800):
            calls.append((list(args), cwd))
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return RESOURCE_SHA
            if args[:2] == ["git", "status"]:
                return ""
            if args[0] == "node" and str(args[1]).endswith("stage-desktop-mcps.mjs") and stage_writes_arc:
                # The engine's staging script writes the ARC child and its reviewed manifest into the destination.
                destination = Path(args[4])
                for name in resources.COMPANION_ARC_FILES:
                    (destination / name).parent.mkdir(parents=True, exist_ok=True)
                    (destination / name).write_bytes(b"synthetic")
            return ""

        git_root = self.root / "git"
        (git_root / "cmd").mkdir(parents=True, exist_ok=True)
        (git_root / "bin").mkdir(parents=True, exist_ok=True)
        (git_root / "cmd" / "git.exe").write_bytes(b"")
        (git_root / "bin" / "bash.exe").write_bytes(b"")
        if platform == "darwin-arm64":
            bundle = self.source / "dist" / "Companion_1.2.3_darwin_arm64.app" / "Contents" / "Resources"
            bundle.mkdir(parents=True, exist_ok=True)
            (bundle / "AppIcon.icns").write_bytes(b"icon")
        env = dict(self.env)
        with patch.object(resources, "extract_runtimes"), \
                patch.object(resources, "check_macos_runtimes"), \
                patch.object(resources, "checked", side_effect=fake_checked), \
                patch.object(resources.shutil, "which", return_value=str(git_root / "cmd" / "git.exe")):
            resources.stage_resources(self.source, env, "v1.2.3", platform)
        return calls

    def index(self, calls, predicate):
        matches = [i for i, (args, cwd) in enumerate(calls) if predicate(args, cwd)]
        self.assertEqual(len(matches), 1, f"expected exactly one matching command, got {len(matches)}")
        return matches[0]

    def assert_arc_built_before_assembly(self, platform, npm):
        calls = self.run_stage(platform)
        legacy = self.workspace / "resource-source"
        # The desktop checkout (legacy) installs its dependencies, builds the ARC child, and only then the engine's
        # staging script assembles the package. The connectors' own npm ci runs in legacy/connectors and is not this one.
        install = self.index(calls, lambda a, cwd: a[:3] == [npm, "ci", "--ignore-scripts"] and cwd == legacy)
        build = self.index(calls, lambda a, cwd: a == ["node", "scripts/build-arc-child.mjs"] and cwd == legacy)
        assembly = self.index(calls, lambda a, cwd: a[0] == "node" and str(a[1]).endswith("stage-desktop-mcps.mjs"))
        self.assertLess(install, build)
        self.assertLess(build, assembly)

    def test_windows_installs_and_builds_the_arc_child_before_assembling_the_package(self):
        self.assert_arc_built_before_assembly("windows-amd64", "npm.cmd")

    def test_macos_installs_and_builds_the_arc_child_before_assembling_the_package(self):
        self.assert_arc_built_before_assembly("darwin-arm64", "npm")

    def test_a_package_without_the_arc_child_after_assembly_stops_the_stage(self):
        # Incident guard: the engine recognises the browser only by the staged file, so a staging step that writes
        # nothing must end the stage with its own status (29), not produce a package that runs without the browser.
        for platform in ("windows-amd64", "darwin-arm64"):
            with self.subTest(platform=platform):
                with self.assertRaises(resources.ResourceStageError) as failure:
                    self.run_stage(platform, stage_writes_arc=False)
                self.assertEqual(failure.exception.status, 29)

    def test_check_companion_arc_requires_both_files_and_rejects_empty_ones(self):
        resources_dir = self.root / "resources"
        with self.assertRaisesRegex(ValueError, "companion-arc.cjs"):
            resources.check_companion_arc(resources_dir)
        for name in resources.COMPANION_ARC_FILES:
            (resources_dir / name).parent.mkdir(parents=True, exist_ok=True)
            (resources_dir / name).write_bytes(b"x")
        resources.check_companion_arc(resources_dir)
        (resources_dir / "default-mcps/companion-arc/mcp.json").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "mcp.json"):
            resources.check_companion_arc(resources_dir)


if __name__ == "__main__":
    unittest.main()
