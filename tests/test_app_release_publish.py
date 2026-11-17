import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import app_release_publish
from app_release_publish import draft, marker, finalize, upload
from app_release_artifacts import digest, filenames, prepare


class FakeGitHub:
    """The releases API as GitHub serves it to a credential that can push.

    GET /releases/tags/{tag} answers 404 for a draft: it returns published releases only. This was checked
    against a real draft (aios-companion-releases v0.16.3) on 17 Nov 2026; a published tag (v0.18.0) was found.
    The listing includes drafts.
    """

    def __init__(self, target, releases=()):
        self.target = target
        self.releases = [dict(release) for release in releases]
        self.calls = []
        self.metadata = dict(id=123456, full_name=target, private=False, visibility="public", archived=False,
                             disabled=False, owner={"login": "AIOSCompanion"}, default_branch="main",
                             permissions={"push": True})
        self.after_create = None

    def __call__(self, method, path, payload=None, **kwargs):
        self.calls.append((method, path))
        root = f"repos/{self.target}"
        if method == "POST" and path == f"{root}/releases":
            release = dict(payload, id=1000 + len(self.releases), assets=[])
            self.releases.append(release)
            if self.after_create:
                self.after_create(self)
            return release
        assert method == "GET", (method, path)
        if path == root:
            return self.metadata
        if path == f"{root}/git/ref/heads/main":
            return {"object": {"type": "commit", "sha": "b" * 40}}
        if path.startswith(f"{root}/releases/tags/"):
            tag = path.rsplit("/", 1)[1]
            published = [r for r in self.releases if r["tag_name"] == tag and not r["draft"]]
            if not published:
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return published[0]
        if path.startswith(f"{root}/releases?"):
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            size, page = int(query["per_page"]), int(query["page"])
            return self.releases[(page - 1) * size:page * size]
        if path.startswith(f"{root}/releases/"):
            wanted = int(path.rsplit("/", 1)[1])
            return next(r for r in self.releases if r["id"] == wanted)
        raise AssertionError(path)

    def posts(self):
        return [call for call in self.calls if call[0] == "POST"]


class AppDraftTests(unittest.TestCase):
    TARGET = "AIOSCompanion/approved-app-downloads"
    SHA = "a" * 40

    def release(self, **values):
        release = dict(id=7, draft=True, tag_name="v1.2.3", prerelease=False, assets=[],
                       body=marker("v1.2.3", self.SHA, False))
        release.update(values)
        return release

    def test_the_draft_is_found_again_although_githubs_tag_endpoint_hides_drafts(self):
        github = FakeGitHub(self.TARGET)
        created = draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=github)
        self.assertTrue(created["draft"])
        # The platform rows and the final check look the same draft up without creating one.
        self.assertEqual(draft(self.TARGET, "v1.2.3", self.SHA, request=github)["id"], created["id"])
        # A second dispatch reuses it instead of creating a duplicate draft.
        self.assertEqual(draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=github)["id"], created["id"])
        self.assertEqual(len(github.posts()), 1)
        self.assertFalse([path for _, path in github.calls if "/releases/tags/" in path])
        # The instrument itself: the draft really is invisible at the tag endpoint.
        with self.assertRaisesRegex(RuntimeError, "404"):
            github("GET", f"repos/{self.TARGET}/releases/tags/v1.2.3")

    def test_creation_is_always_an_unpublished_draft_on_the_public_repositorys_own_commit(self):
        github = FakeGitHub(self.TARGET)
        draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=github)
        created = github.releases[0]
        self.assertTrue(created["draft"])
        self.assertEqual(created["target_commitish"], "b" * 40)
        self.assertNotEqual(created["target_commitish"], self.SHA)
        self.assertFalse(created["generate_release_notes"])

    def test_a_published_release_with_the_tag_is_never_modified_or_built_into(self):
        for tag in ("v1.2.3", "1.2.3"):
            github = FakeGitHub(self.TARGET, [self.release(draft=False, tag_name=tag)])
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "published"):
                draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=github)
            self.assertFalse(github.posts())

    def test_two_drafts_for_one_tag_are_refused_and_a_concurrent_creator_stops_the_run(self):
        github = FakeGitHub(self.TARGET, [self.release(id=7), self.release(id=8)])
        with self.assertRaisesRegex(ValueError, "More than one draft"):
            draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=github)
        racing = FakeGitHub(self.TARGET)
        racing.after_create = lambda fake: fake.releases.append(dict(fake.releases[-1], id=9999))
        with self.assertRaisesRegex(ValueError, "More than one draft"):
            draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=racing)

    def test_a_missing_draft_is_never_created_by_an_upload_or_the_final_check(self):
        github = FakeGitHub(self.TARGET)
        with self.assertRaisesRegex(ValueError, "missing"):
            draft(self.TARGET, "v1.2.3", self.SHA, request=github)
        self.assertFalse(github.posts())

    def test_a_listing_that_reaches_its_bound_is_a_refusal(self):
        others = [self.release(id=i, tag_name=f"v0.0.{i}") for i in range(
            app_release_publish.LISTING_PAGES * app_release_publish.PAGE_SIZE)]
        with self.assertRaisesRegex(ValueError, "bound"):
            draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=FakeGitHub(self.TARGET, others))

    def test_a_different_source_or_signing_mode_is_never_modified(self):
        for values in [dict(body=marker("v1.2.3", "c" * 40, False)), dict(body=marker("v1.2.3", self.SHA, True))]:
            github = FakeGitHub(self.TARGET, [self.release(**values)])
            with self.subTest(values=values), self.assertRaises(ValueError):
                draft(self.TARGET, "v1.2.3", self.SHA, request=github)
            self.assertFalse(github.posts())

    def test_unsigned_mode_requires_a_labelled_prerelease_draft(self):
        github = FakeGitHub(self.TARGET)
        draft(self.TARGET, "v1.2.3", self.SHA, True, create=True, request=github)
        created = github.releases[0]
        self.assertTrue(created["prerelease"])
        self.assertTrue(created["draft"])
        self.assertIn("UNSIGNED", created["name"])
        self.assertIn("UNSIGNED", created["body"])

    def test_ambiguous_multiple_identity_markers_cannot_reuse_a_draft(self):
        body = marker("v1.2.3", self.SHA, False) + "\n" + marker("v1.2.3", "c" * 40, False)
        with self.assertRaises(ValueError):
            draft(self.TARGET, "v1.2.3", self.SHA, request=FakeGitHub(self.TARGET, [self.release(body=body)]))

    def test_different_existing_asset_bytes_fail_before_any_upload_or_overwrite(self):
        release = {"assets": [{"name": "synthetic.exe", "digest": "sha256:" + "b" * 64}]}
        with patch("app_release_publish.inspect", return_value=({}, [Path("synthetic.exe")])), \
                patch("app_release_publish.draft", return_value=release), \
                patch("app_release_publish.digest", return_value="a" * 64), \
                patch("app_release_publish.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "different bytes"):
                upload(self.TARGET, "v1.2.3", self.SHA, "unused", "windows-amd64")
            execute.assert_not_called()

    def test_incomplete_or_additional_asset_sets_never_finalize_a_draft(self):
        with patch("app_release_publish.draft", return_value={"assets": []}), \
                patch("app_release_publish.api") as api, \
                patch("app_release_publish.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "complete platform"):
                finalize(self.TARGET, "v1.2.3", self.SHA)
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
                finalize(self.TARGET, "v1.2.3", self.SHA)
                self.assertEqual(api.call_count, 1)
                method, path, payload = api.call_args.args
                self.assertEqual((method, path), ("PATCH", f"repos/{self.TARGET}/releases/77"))
                self.assertIs(payload["draft"], True)
                api.reset_mock()
                next(asset for asset in assets if asset["name"].endswith(".exe"))["digest"] = "sha256:" + "0" * 64
                with self.assertRaisesRegex(ValueError, "do not match"):
                    finalize(self.TARGET, "v1.2.3", self.SHA)
                api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
