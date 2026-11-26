import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import app_release_identity
import bridge_release
from bridge_release import (BRIDGE_DESTINATION, bridge_version, build, draft, fetch, parse_latest, verify_outputs, _child_env)

# The feed the 0.18.0 Electron release published, as GitHub serves it (aios-companion-releases, 15 Nov 2026).
LATEST_0_18_0 = """version: 0.18.0
files:
  - url: Companion-Setup-x64-0.18.0.exe
    sha512: SiBNwRyThYqWm6W1xyeRK1cSrXsCjhmYsxkSbI1I9Uv/pBMSnMW3EmDPQ/f+W9GBt42n5S74N4jDJ7V2e9zQOQ==
    size: 450708044
path: Companion-Setup-x64-0.18.0.exe
sha512: SiBNwRyThYqWm6W1xyeRK1cSrXsCjhmYsxkSbI1I9Uv/pBMSnMW3EmDPQ/f+W9GBt42n5S74N4jDJ7V2e9zQOQ==
releaseDate: '2026-11-15T03:48:15.500Z'
"""


def sha512_b64(data):
    return base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def feed_for(value, installer, data):
    name = f"Companion-Setup-x64-{value}.exe"
    digest = sha512_b64(data)
    return (f"version: {value}\nfiles:\n  - url: {name}\n    sha512: {digest}\n    size: {len(data)}\n"
            f"path: {name}\nsha512: {digest}\nreleaseDate: '2026-11-26T00:00:00.000Z'\n")


def blockmap_for(name):
    return gzip.compress(json.dumps({"version": 2, "files": [{"name": "file", "offset": 0,
                                                             "checksums": ["AAAA"], "sizes": [1]}]}).encode())


def write_outputs(folder, value, installer=b"installer-bytes", feed=None, blockmap=None):
    name = f"Companion-Setup-x64-{value}.exe"
    (folder / name).write_bytes(installer)
    (folder / f"{name}.blockmap").write_bytes(blockmap if blockmap is not None else blockmap_for(name))
    (folder / "latest.yml").write_text(feed if feed is not None else feed_for(value, name, installer), encoding="utf-8")
    return folder


class FakeGitHub:
    """A releases API that serves the tag's releases, the destination metadata and the latest release."""

    def __init__(self, releases=(), latest="0.18.0", repo=BRIDGE_DESTINATION):
        self.releases = [dict(r) for r in releases]
        self.latest = latest
        self.repo = repo
        self.calls = []

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path))
        if path == "repos/" + self.repo:
            return {"full_name": self.repo, "id": 1, "private": False, "archived": False, "disabled": False,
                    "owner": {"login": "AIOSCompanion"}, "visibility": "public", "permissions": {"push": True}}
        if path.startswith("repos/" + self.repo + "/releases/latest"):
            return {"tag_name": self.latest}
        if path.startswith("repos/" + self.repo + "/releases?"):
            return list(self.releases)
        raise AssertionError(f"unexpected {method} {path}")


class BridgeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_bridge_names_its_own_destination_and_the_go_pipeline_still_refuses_it(self):
        # The Go app's pipeline must keep refusing the updater's repository; only this module may draft there.
        self.assertEqual(BRIDGE_DESTINATION, "AIOSCompanion/aios-companion-releases")
        self.assertIn(BRIDGE_DESTINATION, app_release_identity.FORBIDDEN_TARGETS)

    def test_only_a_plain_release_tag_is_bridged(self):
        self.assertEqual(bridge_version("v0.19.2"), "0.19.2")
        for tag in ("v0.19.2-rc.1", "v0.19", "0.19.2", "v0.19.2.1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                bridge_version(tag)

    def test_the_real_0_18_0_feed_parses_to_the_electron_updater_shape(self):
        feed = parse_latest(LATEST_0_18_0)
        self.assertEqual(feed["version"], "0.18.0")
        self.assertEqual(feed["path"], "Companion-Setup-x64-0.18.0.exe")
        self.assertEqual(feed["file"], {"url": "Companion-Setup-x64-0.18.0.exe",
                                        "sha512": "SiBNwRyThYqWm6W1xyeRK1cSrXsCjhmYsxkSbI1I9Uv/pBMSnMW3EmDPQ/f+W9GBt42n5S74N4jDJ7V2e9zQOQ==",
                                        "size": "450708044"})
        # The same structure, read by a YAML parser: the parser and the line reader agree.
        loaded = yaml.safe_load(LATEST_0_18_0)
        self.assertEqual(loaded["files"][0]["size"], int(feed["file"]["size"]))

    def test_a_feed_with_another_shape_is_refused(self):
        with self.assertRaises(ValueError):
            parse_latest(LATEST_0_18_0.replace("  - url:", "  - url: a\n  - url:"))

    def test_a_consistent_trio_of_outputs_is_accepted(self):
        out = write_outputs(self.folder, "0.19.2")
        files = verify_outputs(out, "0.19.2")
        self.assertEqual(sorted(files), ["Companion-Setup-x64-0.19.2.exe", "Companion-Setup-x64-0.19.2.exe.blockmap", "latest.yml"])

    def test_an_extra_file_in_the_output_is_refused(self):
        out = write_outputs(self.folder, "0.19.2")
        (out / "payload.zip").write_bytes(b"local-only")
        with self.assertRaises(ValueError):
            verify_outputs(out, "0.19.2")

    def test_a_feed_that_does_not_carry_the_installer_hash_is_refused(self):
        installer = b"installer-bytes"
        # Same length, other content: only the hash can tell them apart.
        feed = feed_for("0.19.2", "Companion-Setup-x64-0.19.2.exe", b"x" * len(installer))
        out = write_outputs(self.folder, "0.19.2", installer=installer, feed=feed)
        with self.assertRaises(ValueError):
            verify_outputs(out, "0.19.2")

    def test_a_blockmap_that_is_not_the_electron_format_is_refused(self):
        out = write_outputs(self.folder, "0.19.2", blockmap=gzip.compress(b'{"version": 1, "files": []}'))
        with self.assertRaises(ValueError):
            verify_outputs(out, "0.19.2")

    def test_fetch_takes_the_go_release_only_when_each_asset_matches_github_and_the_checksum_list(self):
        zip_bytes = b"verified-archive"
        receipt = b'{"receipt": true}'
        zip_digest = hashlib.sha256(zip_bytes).hexdigest()
        sums = f"{zip_digest}  Companion_0.19.2_windows_amd64.zip\n".encode()
        contents = {"Companion_0.19.2_windows_amd64.zip": zip_bytes,
                    "companion-release-windows-amd64.json": receipt,
                    "SHA256SUMS-windows-amd64.txt": sums}
        assets = [{"name": n, "id": i, "digest": "sha256:" + hashlib.sha256(c).hexdigest()}
                  for i, (n, c) in enumerate(contents.items(), start=10)]
        release = {"tag_name": "v0.19.2", "draft": True, "assets": assets}

        def request(method, path):
            if "releases?" in path:
                return [release]
            raise AssertionError(path)

        def download(repository, asset_id, path):
            name = next(a["name"] for a in assets if a["id"] == asset_id)
            Path(path).write_bytes(contents[name])

        names = fetch("v0.19.2", "AIOSCompanion/companion-app-releases", self.folder / "in", request, download)
        self.assertEqual(sorted(names), sorted(contents))

    def test_fetch_refuses_an_archive_that_the_checksum_list_does_not_name(self):
        zip_bytes = b"tampered"
        contents = {"Companion_0.19.2_windows_amd64.zip": zip_bytes, "companion-release-windows-amd64.json": b"{}",
                    "SHA256SUMS-windows-amd64.txt": b"0" * 64 + b"  Companion_0.19.2_windows_amd64.zip\n"}
        assets = [{"name": n, "id": i, "digest": "sha256:" + hashlib.sha256(c).hexdigest()}
                  for i, (n, c) in enumerate(contents.items(), start=1)]

        def request(method, path):
            return [{"tag_name": "v0.19.2", "assets": assets}]

        def download(repository, asset_id, path):
            name = next(a["name"] for a in assets if a["id"] == asset_id)
            Path(path).write_bytes(contents[name])

        with self.assertRaises(ValueError):
            fetch("v0.19.2", "AIOSCompanion/companion-app-releases", self.folder / "in", request, download)

    def test_fetch_refuses_a_go_tag_with_two_releases(self):
        release = {"tag_name": "v0.19.2", "draft": True, "assets": []}
        with self.assertRaises(ValueError):
            fetch("v0.19.2", "AIOSCompanion/companion-app-releases", self.folder / "in",
                  lambda m, p: [release, dict(release)], lambda *a: None)

    def test_draft_creates_only_an_unpublished_release_and_checks_it(self):
        out = write_outputs(self.folder, "0.19.2")
        files = verify_outputs(out, "0.19.2")
        github = FakeGitHub()
        created = {}

        def create(args, **kwargs):
            created["args"] = args
            digests = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files.values()}
            github.releases = [{"id": 7, "tag_name": "v0.19.2", "draft": True, "prerelease": False,
                                "assets": [{"name": p.name, "size": p.stat().st_size, "digest": "sha256:" + digests[p.name]}
                                           for p in files.values()]}]
            return subprocess.CompletedProcess(args, 0)

        with patch.dict(os.environ, {"GH_TOKEN": "test-only"}):
            result = draft("v0.19.2", out, github, create)
        self.assertEqual(result["draft_id"], 7)
        self.assertIn("--draft", created["args"])
        self.assertNotIn("--latest", created["args"])
        self.assertNotIn("edit", created["args"])
        self.assertTrue(all(method == "GET" for method, _ in github.calls))

    def test_draft_refuses_a_destination_that_already_has_the_tag(self):
        out = write_outputs(self.folder, "0.19.2")
        github = FakeGitHub(releases=[{"tag_name": "v0.19.2", "draft": True, "assets": []}])

        def create(*args, **kwargs):
            raise AssertionError("the draft must not be created when the tag is taken")

        with self.assertRaises(ValueError) as refused:
            draft("v0.19.2", out, github, create)
        self.assertIn("already has a release", str(refused.exception))

    def test_draft_refuses_a_created_release_that_is_not_a_draft(self):
        out = write_outputs(self.folder, "0.19.2")
        files = verify_outputs(out, "0.19.2")
        github = FakeGitHub()

        def create(args, **kwargs):
            github.releases = [{"id": 8, "tag_name": "v0.19.2", "draft": False, "prerelease": False,
                                "assets": [{"name": p.name, "size": p.stat().st_size,
                                            "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()}
                                           for p in files.values()]}]
            return subprocess.CompletedProcess(args, 0)

        with self.assertRaises(ValueError):
            draft("v0.19.2", out, github, create)

    def test_draft_refuses_a_changed_latest_release(self):
        out = write_outputs(self.folder, "0.19.2")
        files = verify_outputs(out, "0.19.2")
        github = FakeGitHub()

        def create(args, **kwargs):
            github.releases = [{"id": 9, "tag_name": "v0.19.2", "draft": True, "prerelease": False,
                                "assets": [{"name": p.name, "size": p.stat().st_size,
                                            "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()}
                                           for p in files.values()]}]
            github.latest = "0.19.2"
            return subprocess.CompletedProcess(args, 0)

        with self.assertRaises(ValueError):
            draft("v0.19.2", out, github, create)

    def test_the_build_refuses_to_run_holding_a_credential(self):
        with self.assertRaises(ValueError):
            build({"TAG": "v0.19.2", "GH_TOKEN": "x"})

    def test_the_build_environment_has_no_token_of_any_kind(self):
        child = _child_env({"TAG": "v0.19.2", "PUBLISH_RELEASES_TOKEN": "x", "GITHUB_TOKEN": "y", "PATH": "p"})
        self.assertNotIn("PUBLISH_RELEASES_TOKEN", child)
        self.assertNotIn("GITHUB_TOKEN", child)
        self.assertEqual(child["GOTOOLCHAIN"], "local")
        self.assertEqual(child["PATH"], "p")


class BridgeWorkflowTests(unittest.TestCase):
    WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/bridge-release.yml").read_text())

    def steps(self):
        return self.WORKFLOW["jobs"]["bridge-windows"]["steps"]

    def test_only_an_owner_dispatch_from_builder_main_enters_the_job(self):
        trigger = self.WORKFLOW.get("on", self.WORKFLOW.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        job = self.WORKFLOW["jobs"]["bridge-windows"]
        self.assertIn("github.repository == 'AIOSCompanion/companion-builder'", job["if"])
        self.assertIn("github.ref == 'refs/heads/main'", job["if"])
        self.assertEqual(job["environment"], "release-signing")
        self.assertEqual(self.WORKFLOW["permissions"], {"contents": "read"})

    def test_the_publishing_token_is_only_in_fetch_and_draft_steps_and_never_in_a_build(self):
        for step in self.steps():
            text = str(step.get("env", {}))
            if "PUBLISH_RELEASES_TOKEN" in text:
                self.assertTrue(any(command in step.get("run", "") for command in ('bridge_release.py" fetch', 'bridge_release.py" draft')),
                                step.get("name"))
                self.assertIn("run_sealed.py", step["run"])

    def test_the_fetch_runs_before_any_private_source_is_present(self):
        names = [step.get("name", "") for step in self.steps()]
        fetch_index = next(i for i, step in enumerate(self.steps()) if 'bridge_release.py" fetch' in step.get("run", ""))
        checkout_index = next(i for i, step in enumerate(self.steps()) if "app_release.py\" checkout" in step.get("run", ""))
        self.assertLess(fetch_index, checkout_index, names)

    def test_the_build_step_has_no_credential_and_runs_the_reviewed_bridge_script(self):
        build_steps = [s for s in self.steps() if 'bridge_release.py" build' in s.get("run", "")]
        self.assertEqual(len(build_steps), 1)
        for key in ("GH_TOKEN", "PUBLISH_RELEASES_TOKEN", "GITHUB_TOKEN"):
            self.assertNotIn(key, str(build_steps[0].get("env", {})))

    def test_the_workflow_never_publishes_or_edits_a_release(self):
        text = (ROOT / ".github/workflows/bridge-release.yml").read_text()
        for forbidden in ("release edit", "--draft=false", "--latest", "--prerelease=false", "release publish"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
