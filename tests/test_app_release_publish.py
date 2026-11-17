import json
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_publish import draft, marker, finalize, upload
from app_release_artifacts import digest, filenames, prepare


class AppDraftTests(unittest.TestCase):
    TARGET = "AIOSCompanion/approved-app-downloads"
    SHA = "a" * 40

    def metadata(self):
        return dict(id=123456, full_name=self.TARGET, private=False, visibility="public", archived=False,
                    disabled=False, owner={"login": "AIOSCompanion"}, default_branch="main")

    def test_creation_is_always_an_unpublished_draft_on_the_public_repositories_own_commit(self):
        mutations = []
        def api(method, path, payload=None, **kwargs):
            if method == "POST":
                mutations.append(payload)
                self.assertTrue(payload["draft"])
                self.assertEqual(payload["target_commitish"], "b" * 40)
                return dict(payload, id=55)
            if path.endswith("/git/ref/heads/main"):
                return {"object": {"type": "commit", "sha": "b" * 40}}
            if path.endswith("/releases/tags/v1.2.3"):
                return None
            return self.metadata()
        result = draft(self.TARGET, "123456", "v1.2.3", self.SHA, create=True, request=api)
        self.assertTrue(result["draft"])
        self.assertEqual(len(mutations), 1)
        self.assertNotEqual(mutations[0]["target_commitish"], self.SHA)

    def test_published_or_different_source_or_signing_mode_is_never_modified(self):
        for values in [dict(draft=False), dict(body=marker("v1.2.3", "c" * 40, False)),
                       dict(body=marker("v1.2.3", self.SHA, True))]:
            release = dict(draft=True, tag_name="v1.2.3", body=marker("v1.2.3", self.SHA, False))
            release.update(values)
            def api(method, path, payload=None, **kwargs):
                self.assertEqual(method, "GET")
                return release if "/releases/" in path else self.metadata()
            with self.subTest(values=values), self.assertRaises(ValueError):
                draft(self.TARGET, "123456", "v1.2.3", self.SHA, request=api)

    def test_unsigned_mode_requires_a_labelled_prerelease_draft(self):
        seen = []
        def api(method, path, payload=None, **kwargs):
            if method == "POST":
                seen.append(payload)
                return dict(payload, id=1)
            if path.endswith("/releases/tags/v1.2.3"):
                return None
            if "/git/ref/" in path:
                return {"object": {"type": "commit", "sha": "b" * 40}}
            return self.metadata()
        draft(self.TARGET, "123456", "v1.2.3", self.SHA, True, create=True, request=api)
        self.assertTrue(seen[0]["prerelease"])
        self.assertTrue(seen[0]["draft"])
        self.assertIn("UNSIGNED", seen[0]["name"])

    def test_ambiguous_multiple_identity_markers_cannot_reuse_a_draft(self):
        release = dict(draft=True, tag_name="v1.2.3", body=marker("v1.2.3", self.SHA, False)
                       + "\n" + marker("v1.2.3", "c" * 40, False))
        def api(method, path, **kwargs):
            return release if "/releases/" in path else self.metadata()
        with self.assertRaises(ValueError):
            draft(self.TARGET, "123456", "v1.2.3", self.SHA, request=api)

    def test_different_existing_asset_bytes_fail_before_any_upload_or_overwrite(self):
        release = {"assets": [{"name": "synthetic.exe", "digest": "sha256:" + "b" * 64}]}
        with patch("app_release_publish.inspect", return_value=({}, [Path("synthetic.exe")])), \
                patch("app_release_publish.draft", return_value=release), \
                patch("app_release_publish.digest", return_value="a" * 64), \
                patch("app_release_publish.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "different bytes"):
                upload(self.TARGET, "123456", "v1.2.3", self.SHA, "unused", "windows-amd64")
            execute.assert_not_called()

    def test_incomplete_or_additional_asset_sets_never_finalize_a_draft(self):
        with patch("app_release_publish.draft", return_value={"assets": []}), \
                patch("app_release_publish.api") as api, \
                patch("app_release_publish.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "complete platform"):
                finalize(self.TARGET, "123456", "v1.2.3", self.SHA)
            api.assert_not_called()
            execute.assert_not_called()

    def test_finalization_binds_both_real_staged_receipts_and_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            assets, downloads = [], {}
            for platform in ("windows-amd64", "darwin-arm64"):
                root = Path(directory) / platform
                root.mkdir()
                names = filenames("v1.2.3", platform)
                for name in names:
                    (root / name).write_bytes(b"synthetic " + name.encode())
                if platform == "windows-amd64":
                    (root / "SHA256SUMS").write_text("".join(f"{digest(root / name)}  {name}\n" for name in names))
                prepare(root, root / "staged", "v1.2.3", self.SHA, platform)
                for file in (root / "staged").iterdir():
                    id = len(assets) + 1
                    assets.append(dict(id=id, name=file.name, size=file.stat().st_size, digest="sha256:" + digest(file)))
                    downloads[str(id)] = file.read_bytes()
            release = dict(id=77, assets=assets, body=marker("v1.2.3", self.SHA, False))
            def download(args, **kwargs):
                return SimpleNamespace(returncode=0, stdout=downloads[args[-1].rsplit("/", 1)[-1]])
            with patch("app_release_publish.draft", return_value=release), \
                    patch("app_release_publish.subprocess.run", side_effect=download), \
                    patch("app_release_publish.api") as api:
                finalize(self.TARGET, "123456", "v1.2.3", self.SHA)
                self.assertEqual(api.call_count, 1)
                method, path, payload = api.call_args.args
                self.assertEqual((method, path), ("PATCH", f"repos/{self.TARGET}/releases/77"))
                self.assertIs(payload["draft"], True)
                api.reset_mock()
                next(asset for asset in assets if asset["name"].endswith(".exe"))["digest"] = "sha256:" + "0" * 64
                with self.assertRaisesRegex(ValueError, "do not match"):
                    finalize(self.TARGET, "123456", "v1.2.3", self.SHA)
                api.assert_not_called()
