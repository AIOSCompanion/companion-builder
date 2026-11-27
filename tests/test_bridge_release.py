import base64
import contextlib
import gzip
import hashlib
import io
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
from app_release_artifacts import RECEIPTS
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
        receipt = receipt_bytes("v0.19.2")
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
        self.assertEqual(names, ["Companion_0.19.2_windows_amd64.zip", "companion-release-windows-amd64.json",
                                 "SHA256SUMS-windows-amd64.txt"])

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


G1 = RECEIPTS["windows-amd64"][1]
G2 = RECEIPTS["windows-amd64"][2]


def receipt_bytes(tag, version=None):
    """A receipt as the builder writes it, for the release tag (its version defaults to the tag's)."""
    value = version or tag[1:]
    return json.dumps({"schema": 1, "platform": "windows-amd64", "tag": tag, "version": value, "signing": "unsigned-windows",
                       "source_sha": "a" * 40, "assets": {f"Companion_{value}_windows_amd64.zip": "0" * 64,
                                                           "SHA256SUMS-windows-amd64.txt": "1" * 64}}, indent=2).encode()


def go_release(tag, receipts, zip_bytes=b"verified-archive"):
    """A Go release's draft as GitHub serves it: the archive, its checksum list and the given receipts, with GitHub's digests."""
    zip_name = f"Companion_{tag[1:]}_windows_amd64.zip"
    contents = {zip_name: zip_bytes,
                "SHA256SUMS-windows-amd64.txt": f"{hashlib.sha256(zip_bytes).hexdigest()}  {zip_name}\n".encode(), **receipts}
    assets = [{"name": n, "id": i, "digest": "sha256:" + hashlib.sha256(c).hexdigest()}
              for i, (n, c) in enumerate(contents.items(), start=10)]
    release = {"tag_name": tag, "draft": True, "assets": assets}

    def request(method, path):
        if "releases?" in path:
            return [release]
        raise AssertionError(path)

    def download(repository, asset_id, path):
        name = next(a["name"] for a in assets if a["id"] == asset_id)
        Path(path).write_bytes(contents[name])

    return request, download


class ReceiptSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fetch_release(self, tag, receipts):
        request, download = go_release(tag, receipts)
        return fetch(tag, "AIOSCompanion/companion-app-releases", self.folder / "in", request, download)

    def test_a_v0_19_5_shaped_release_takes_generation_two_not_the_generation_one_copy_of_v0_19_4(self):
        names = self.fetch_release("v0.19.5", {G1: receipt_bytes("v0.19.4"), G2: receipt_bytes("v0.19.5")})
        self.assertEqual(names[1], G2)
        self.assertEqual(bridge_release.select_receipt(self.folder / "in", "v0.19.5"), G2)

    def test_when_both_generations_name_the_release_the_higher_generation_is_taken(self):
        names = self.fetch_release("v0.19.5", {G1: receipt_bytes("v0.19.5"), G2: receipt_bytes("v0.19.5")})
        self.assertEqual(names[1], G2)

    def test_an_older_single_receipt_release_still_bridges(self):
        names = self.fetch_release("v0.19.2", {G1: receipt_bytes("v0.19.2")})
        self.assertEqual(names, ["Companion_0.19.2_windows_amd64.zip", G1, "SHA256SUMS-windows-amd64.txt"])

    def test_a_release_that_no_receipt_names_is_refused(self):
        with self.assertRaisesRegex(ValueError, "No Go release receipt names v0.19.5"):
            self.fetch_release("v0.19.5", {G1: receipt_bytes("v0.19.4"), G2: receipt_bytes("v0.19.4")})

    def test_a_receipt_whose_version_is_another_is_not_taken_for_the_release(self):
        with self.assertRaisesRegex(ValueError, "No Go release receipt names"):
            self.fetch_release("v0.19.5", {G2: receipt_bytes("v0.19.5", version="0.19.4")})

    def test_a_release_with_no_receipt_at_all_is_refused(self):
        with self.assertRaisesRegex(ValueError, "No Go release receipt names"):
            self.fetch_release("v0.19.5", {})

    def test_a_receipt_that_does_not_read_is_refused_rather_than_skipped(self):
        with self.assertRaisesRegex(ValueError, "is not a readable receipt"):
            self.fetch_release("v0.19.5", {G2: b"not json", G1: receipt_bytes("v0.19.5")})

    def test_build_hands_package_bridge_the_generation_two_receipt_and_the_archive(self):
        inp = self.folder / "in"
        inp.mkdir()
        (inp / "Companion_0.19.5_windows_amd64.zip").write_bytes(b"zip")
        (inp / G1).write_bytes(receipt_bytes("v0.19.4"))
        (inp / G2).write_bytes(receipt_bytes("v0.19.5"))
        source = self.folder / "source"
        (source / "scripts").mkdir(parents=True)
        (source / "scripts" / "package-bridge.ps1").write_text("# reviewed script\n", encoding="utf-8")
        calls = []

        def run(args, **kwargs):
            calls.append(args)
            write_outputs(Path(args[args.index("-OutDir") + 1]), "0.19.5")
            return subprocess.CompletedProcess(args, 0)

        env = {"TAG": "v0.19.5", "BRIDGE_INPUT_DIR": str(inp), "BRIDGE_OUT_DIR": str(self.folder / "out")}
        with patch("app_release.source_identity", return_value=(source, None, None)):
            build(env, run=run)
        args = calls[0]
        self.assertEqual(Path(args[args.index("-Receipt") + 1]).name, G2)
        self.assertEqual(Path(args[args.index("-Archive") + 1]).name, "Companion_0.19.5_windows_amd64.zip")

    def test_build_refuses_when_no_receipt_names_the_release(self):
        inp = self.folder / "in"
        inp.mkdir()
        (inp / "Companion_0.19.5_windows_amd64.zip").write_bytes(b"zip")
        (inp / G1).write_bytes(receipt_bytes("v0.19.4"))
        (inp / G2).write_bytes(receipt_bytes("v0.19.4"))
        env = {"TAG": "v0.19.5", "BRIDGE_INPUT_DIR": str(inp), "BRIDGE_OUT_DIR": str(self.folder / "out")}
        with patch("app_release.source_identity", return_value=(self.folder, None, None)):
            with self.assertRaisesRegex(ValueError, "No Go release receipt names"):
                build(env, run=lambda *a, **k: self.fail("package-bridge must not run"))


def selection_outcome():
    """The selection tests' result, run quietly."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReceiptSelectionTests)
    return unittest.TextTestRunner(stream=io.StringIO()).run(suite)


class ReceiptSelectionMutantTests(unittest.TestCase):
    """Each mutant is a plausible wrong selection. The selection tests must fail under every one, and pass on the real code."""

    original_select = staticmethod(bridge_release.select_receipt)

    def test_the_real_selection_passes_the_selection_tests(self):
        self.assertTrue(selection_outcome().wasSuccessful())

    def assert_killed(self, label, replacements):
        with contextlib.ExitStack() as stack:
            for attribute, replacement in replacements.items():
                stack.enter_context(patch.object(bridge_release, attribute, replacement))
            outcome = selection_outcome()
        self.assertFalse(outcome.wasSuccessful(), f"mutant survived: {label}")

    def test_mutant_ascending_order_is_killed(self):
        def ascending():
            return [RECEIPTS["windows-amd64"][g] for g in sorted(RECEIPTS["windows-amd64"])]
        self.assert_killed("ascending generations", {"receipt_candidates": ascending})

    def test_mutant_generation_one_only_is_killed(self):
        self.assert_killed("generation one only", {"receipt_candidates": lambda: [G1]})

    def test_mutant_no_tag_check_is_killed(self):
        def first_present(folder, tag):
            for name in bridge_release.receipt_candidates():
                if (Path(folder) / name).is_file():
                    return name
            raise ValueError(f"No Go release receipt names {tag}")
        self.assert_killed("first receipt present", {"select_receipt": first_present})

    def test_mutant_fallback_to_the_oldest_copy_is_killed(self):
        original = self.original_select

        def fallback(folder, tag):
            try:
                return original(folder, tag)
            except ValueError:
                return bridge_release.receipt_candidates()[-1]
        self.assert_killed("fallback when none names the release", {"select_receipt": fallback})

    def test_mutant_unreadable_receipt_skipped_is_killed(self):
        def skip_unreadable(folder, tag):
            value = tag[1:]
            for name in bridge_release.receipt_candidates():
                path = Path(folder) / name
                if not path.is_file():
                    continue
                try:
                    receipt = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(receipt, dict) and receipt.get("tag") == "v" + value and receipt.get("version") == value:
                    return name
            raise ValueError(f"No Go release receipt names {tag}")
        self.assert_killed("unreadable receipt skipped", {"select_receipt": skip_unreadable})

    def test_mutant_tag_only_without_version_is_killed(self):
        def tag_only(folder, tag):
            for name in bridge_release.receipt_candidates():
                path = Path(folder) / name
                if path.is_file() and json.loads(path.read_text(encoding="utf-8")).get("tag") == tag:
                    return name
            raise ValueError(f"No Go release receipt names {tag}")
        self.assert_killed("tag matched, version not checked", {"select_receipt": tag_only})


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
