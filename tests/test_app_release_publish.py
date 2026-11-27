import contextlib
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import app_release_publish
from app_release_publish import draft, marker, finalize, read_public, upload
from app_release_artifacts import WINDOWS_RESOURCE_FILES, digest, filenames, prepare, windows_binaries
from test_app_release_artifacts import OLDER_TAG, older_generation_one, older_listing


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

    def test_creation_waits_for_the_same_draft_to_appear_in_the_listing(self):
        github = FakeGitHub(self.TARGET)
        transient_reads = 0

        def delayed(method, path, payload=None):
            nonlocal transient_reads
            if method == "GET" and "/releases?" in path and github.releases and transient_reads < 2:
                transient_reads += 1
                return []
            return github(method, path, payload)

        with patch("app_release_publish.time.sleep") as wait:
            result = draft(self.TARGET, "v1.2.3", self.SHA, create=True, request=delayed)
        self.assertEqual(result["id"], github.releases[0]["id"])
        self.assertEqual(len(github.posts()), 1)
        self.assertEqual([call.args for call in wait.call_args_list], [(1,), (2,)])

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
                names = windows_binaries("v1.2.3") if platform == "windows-amd64" else filenames("v1.2.3", platform)
                for name in names:
                    (root / name).write_bytes(b"synthetic " + name.encode())
                if platform == "windows-amd64":
                    (root / "SHA256SUMS").write_text("".join(f"{digest(root / name)}  {name}\n" for name in names))
                    for name in WINDOWS_RESOURCE_FILES:
                        resource = root / name
                        resource.parent.mkdir(parents=True, exist_ok=True)
                        resource.write_bytes(b"synthetic resource")
                prepare(root, root / "staged", "v1.2.3", self.SHA, platform, previous=older_generation_one)
                for file in (root / "staged").iterdir():
                    id = len(assets) + 1
                    assets.append(dict(id=id, name=file.name, size=file.stat().st_size, digest="sha256:" + digest(file)))
                    downloads[str(id)] = file.read_bytes()
            release = dict(id=77, assets=assets, body=marker("v1.2.3", self.SHA, False))
            def download(args, **kwargs):
                return SimpleNamespace(returncode=0, stdout=downloads[args[-1].rsplit("/", 1)[-1]])
            published = {"id": 77, "draft": True, "tag_name": "v1.2.3"}

            def github_api(method, path, payload=None):
                # The Windows generation-1 receipt is a copy of the older release's, which GitHub must still list; the rest is the PATCH.
                if method == "GET" and path == f"repos/{self.TARGET}/releases/tags/{OLDER_TAG}":
                    return older_listing()
                if method == "PATCH":
                    return published
                raise AssertionError((method, path))

            with patch("app_release_publish.draft", return_value=release), \
                    patch("app_release_publish.subprocess.run", side_effect=download), \
                    patch("app_release_publish.api", side_effect=github_api) as api:
                finalize(self.TARGET, "v1.2.3", self.SHA)
                patches = [call.args for call in api.call_args_list if call.args[0] == "PATCH"]
                self.assertEqual(len(patches), 1)
                method, path, payload = patches[0]
                self.assertEqual((method, path), ("PATCH", f"repos/{self.TARGET}/releases/77"))
                self.assertIs(payload["draft"], True)
                self.assertEqual(payload["tag_name"], "v1.2.3")
                published["tag_name"] = "untagged-fixture"
                with self.assertRaisesRegex(ValueError, "retain the identity"):
                    finalize(self.TARGET, "v1.2.3", self.SHA)
                api.reset_mock()
                next(asset for asset in assets if asset["name"].endswith("_windows_amd64.zip"))["digest"] = "sha256:" + "0" * 64
                with self.assertRaisesRegex(ValueError, "do not match"):
                    finalize(self.TARGET, "v1.2.3", self.SHA)
                self.assertFalse(any(call.args[0] == "PATCH" for call in api.call_args_list))


REPO = "AIOSCompanion/approved-app-downloads"
LISTING = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
RECEIPT = "companion-release-windows-amd64.json"
DOWNLOAD = f"https://github.com/{REPO}/releases/download/v1.2.2/{RECEIPT}"
READ_TOKEN = "synthetic-read-only-token"


class TableOpener:
    """Stands in for PUBLIC_OPENER at the network boundary: serves a table of URL to bytes and records every request."""

    def __init__(self, table):
        self.table = table
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if request.full_url not in self.table:
            raise RuntimeError(f"HTTP Error 404: {request.full_url}")
        return contextlib.nullcontext(io.BytesIO(self.table[request.full_url]))


class QuietHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status=200, headers=(), body=b"ok"):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PublicReadTokenTests(unittest.TestCase):
    """The Windows stage step's public reads: the job's read-only token authenticates API calls and nothing else."""

    def test_the_api_read_carries_the_token_and_a_download_address_never_does(self):
        opener = TableOpener({LISTING: b"[]", DOWNLOAD: b"raw"})
        with patch.object(app_release_publish, "PUBLIC_OPENER", opener):
            read_public(LISTING, 100, token=READ_TOKEN)
            read_public(DOWNLOAD, 100, token=READ_TOKEN)
        api, download = opener.requests
        self.assertEqual(api.get_header("Authorization"), "Bearer " + READ_TOKEN)
        self.assertEqual(api.get_header("Accept"), "application/vnd.github+json")
        self.assertFalse(download.has_header("Authorization"))
        self.assertEqual(download.get_header("Accept"), "application/octet-stream")

    def test_without_a_token_every_read_is_anonymous_so_a_local_run_needs_no_credential(self):
        opener = TableOpener({LISTING: b"[]"})
        with patch.object(app_release_publish, "PUBLIC_OPENER", opener):
            read_public(LISTING, 100)
            read_public(LISTING, 100, token="")
        self.assertEqual(len(opener.requests), 2)
        for request in opener.requests:
            self.assertFalse(request.has_header("Authorization"))

    def stage_previous(self, extra_env):
        """The `previous` function that the shipped stage entry point gives previous_receipt for the Windows platform."""
        import app_release
        env = {"TAG": "v1.2.3", "SOURCE_SHA": "a" * 40, "APP_SOURCE_DIR": "source", "APP_ASSET_DIR": "assets",
               "APP_RELEASE_REPOSITORY": REPO, "UNSIGNED_MACOS_TEST": "false", **extra_env}
        captured = {}

        def fake_prepare(dist, asset_dir, tag, sha, platform, unsigned, previous):
            captured["previous"] = previous

        argv = ["app_release.py", "stage", "--platform", "windows-amd64"]
        with patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv), \
                patch.object(app_release, "prepare", fake_prepare), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app_release.main(), 0)
        return captured["previous"]

    def test_the_stage_entry_point_authenticates_the_listing_when_the_job_supplies_a_token(self):
        opener = TableOpener({LISTING: b"[]"})
        with patch.object(app_release_publish, "PUBLIC_OPENER", opener):
            self.assertIsNone(self.stage_previous({"PUBLIC_READ_TOKEN": READ_TOKEN})(RECEIPT))
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + READ_TOKEN)

    def test_the_stage_entry_point_reads_anonymously_when_no_token_is_set(self):
        opener = TableOpener({LISTING: b"[]"})
        with patch.object(app_release_publish, "PUBLIC_OPENER", opener):
            self.assertIsNone(self.stage_previous({})(RECEIPT))
        self.assertFalse(opener.requests[0].has_header("Authorization"))

    def test_the_token_is_not_sent_to_the_download_address_of_the_newest_receipt(self):
        raw = b"not a receipt pointer"
        listing = [{"tag_name": "v1.2.2", "draft": False, "assets": [{
            "name": RECEIPT, "digest": "sha256:" + hashlib.sha256(raw).hexdigest(), "browser_download_url": DOWNLOAD}]}]
        opener = TableOpener({LISTING: json.dumps(listing).encode(), DOWNLOAD: raw})
        previous = self.stage_previous({"PUBLIC_READ_TOKEN": READ_TOKEN})
        with patch.object(app_release_publish, "PUBLIC_OPENER", opener):
            # The bytes then fail the pointer check, a later step. The request order and headers are the point here.
            with self.assertRaisesRegex(ValueError, "not JSON"):
                previous(RECEIPT)
        self.assertEqual([request.full_url for request in opener.requests], [LISTING, DOWNLOAD])
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer " + READ_TOKEN)
        self.assertFalse(opener.requests[1].has_header("Authorization"))

    def test_the_shipped_opener_drops_the_token_on_a_redirect_to_another_host_and_keeps_it_on_the_same_host(self):
        # Real loopback sockets. Two names for the loopback address are two hosts to the redirect guard.
        seen = {"target": [], "origin": []}

        class Target(QuietHandler):
            def do_GET(self):
                seen["target"].append(self.headers.get("Authorization"))
                self.reply()

        class Origin(QuietHandler):
            def do_GET(self):
                if self.path == "/elsewhere":
                    self.reply(302, [("Location", f"http://localhost:{ports['target']}/landing")], b"")
                elif self.path == "/same-host":
                    self.reply(302, [("Location", f"http://127.0.0.1:{ports['origin']}/landing")], b"")
                else:
                    seen["origin"].append(self.headers.get("Authorization"))
                    self.reply()

        ports = {}
        for name, handler in (("target", Target), ("origin", Origin)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            ports[name] = server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        auth = {"Authorization": "Bearer " + READ_TOKEN}
        with app_release_publish.PUBLIC_OPENER.open(Request(f"http://127.0.0.1:{ports['origin']}/elsewhere", headers=auth), timeout=10) as response:
            response.read()
        self.assertEqual(seen["target"], [None], "the token followed a redirect to another host")
        with app_release_publish.PUBLIC_OPENER.open(Request(f"http://127.0.0.1:{ports['origin']}/same-host", headers=auth), timeout=10) as response:
            response.read()
        self.assertEqual(seen["origin"], ["Bearer " + READ_TOKEN], "the token was dropped on a redirect within its own host")


if __name__ == "__main__":
    unittest.main()
