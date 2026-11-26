"""Receipt generations: which Windows receipt names which release, and the copies that keep an older updater valid.

Archives here are real zip files written by the packer's zip writer from staged files. A release that carries the Companion Browser
(ARC) pair is the packer's own archive: it extracts under generation 2 only. A generation-1 release, the stepping stone the 0.19.1 to
0.19.3 updaters read, carries no ARC file: the packer requires the pair, so that archive is written past the packer and prepare() hashes
it as it would a packed one.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import app_release_artifacts
from app_release_artifacts import (GEN1_RESOURCE_FILES, GEN1_RESOURCE_TREES, GEN2_ARC_FILES, GENERATIONS,
                                   WINDOWS_RESOURCE_FILES, WINDOWS_RESOURCE_TREES, archive_names, digest, filenames,
                                   generation_problems, inspect, package_windows, prepare, windows_binaries,
                                   windows_receipts, write_release_archive)
from app_release_publish import finalize, marker, previous_receipt

G1 = "companion-release-windows-amd64.json"
G2 = "companion-release-g2-windows-amd64.json"
CHECKSUM = "SHA256SUMS-windows-amd64.txt"
ARC_FILES = ("resources/companion_arc/companion-arc.cjs", "resources/default-mcps/companion-arc/mcp.json")
REPO = "AIOSCompanion/approved-app-downloads"
NEW, OLD, OLDER, OLDEST = "v1.2.3", "v1.2.2", "v1.2.1", "v1.2.0"
NEW_SHA, OLD_SHA, OLDER_SHA, OLDEST_SHA = "a" * 40, "c" * 40, "d" * 40, "9" * 40
OWN = b'{"this release": true}\n'
LISTING = f"https://api.github.com/repos/{REPO}/releases?per_page=10"


def tags_url(tag):
    return f"https://api.github.com/repos/{REPO}/releases/tags/{tag}"


def download_url(tag, name):
    return f"https://github.com/{REPO}/releases/download/{tag}/{name}"


def stage_build(root, tag):
    """A synthetic Windows build output: the two binaries, their checksum manifest and every resource the packer requires."""
    names = windows_binaries(tag)
    for name in names:
        (root / name).write_bytes(b"synthetic " + name.encode())
    (root / "SHA256SUMS").write_text("".join(f"{digest(root / name)}  {name}\n" for name in names))
    for name in sorted(WINDOWS_RESOURCE_FILES):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(b"synthetic resource " + name.encode())


def archive_of(root, tag, *, omit=()):
    """A release archive from the staged files, written by the packer's zip writer, without the names in omit."""
    members = [*windows_binaries(tag), *sorted(WINDOWS_RESOURCE_FILES)]
    return write_release_archive(root / filenames(tag, "windows-amd64")[0],
                                 {name: root / name for name in members if name not in omit})


def arc_archive(directory, tag):
    """A release archive carrying the ARC pair (generation 2's shape), as a path."""
    root = Path(directory) / "arc-build"
    root.mkdir(parents=True)
    stage_build(root, tag)
    return archive_of(root, tag)


def pointer_to(tag, source_sha):
    """The bytes of a valid copied receipt that names tag. Its archive and checksum hashes are synthetic: a copy is checked against
    GitHub's listing of the release it names (pointer_listing), not against the archive, which this test does not hold."""
    assets = {filenames(tag, "windows-amd64")[0]: "b" * 64, CHECKSUM: "c" * 64}
    receipt = {"schema": 1, "source_sha": source_sha, "tag": tag, "version": tag[1:], "platform": "windows-amd64",
               "signing": "unsigned-windows", "assets": assets}
    return (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")


def pointer_listing(raw):
    """GitHub's listing of the release a copied receipt names: its archive and checksum, with the receipt's hashes."""
    receipt = json.loads(raw)
    return {"tag_name": receipt["tag"], "draft": False, "assets": [
        {"name": name, "digest": "sha256:" + value, "size": 1, "browser_download_url": download_url(receipt["tag"], name)}
        for name, value in receipt["assets"].items()]}


def from_older(generation_one_bytes):
    """A copy source for a release that carries the ARC pair: the older release's generation-1 receipt, and no generation-2 copy."""
    return lambda name: generation_one_bytes if name == G1 else None


def stage_release(directory, tag, sha, previous=None, *, generation=2):
    """Stage a Windows release with prepare() and return its assets folder.

    generation 2 (the default) is the packer's own archive, which carries the ARC pair, so it needs a generation-1 copy. generation 1 is
    the stepping stone: an archive without the ARC pair, written past the packer, which needs a generation-2 copy. Its default copy names
    an older synthetic release, since the stepping stone's own generation-2 receipt is a copy too.
    """
    root = Path(directory) / "build"
    root.mkdir(parents=True)
    stage_build(root, tag)
    output = Path(directory) / "assets"
    if generation == 2:
        prepare(root, output, tag, sha, "windows-amd64", previous=previous)
        return output
    archive = archive_of(root, tag, omit=ARC_FILES)
    if previous is None:
        previous = lambda name: pointer_to(OLDEST, OLDEST_SHA) if name == G2 else None  # noqa: E731
    with patch.object(app_release_artifacts, "package_windows", lambda folder, value: archive):
        prepare(root, output, tag, sha, "windows-amd64", previous=previous)
    return output


def files_of(folder):
    return sorted(Path(folder).iterdir(), key=lambda path: path.name)


def release_json(tag, files, *, draft=False, omit=()):
    """GitHub's JSON for a release whose assets are these files: names, digests and download addresses."""
    return {"tag_name": tag, "draft": draft, "assets": [
        {"name": path.name, "digest": "sha256:" + digest(path), "size": path.stat().st_size,
         "browser_download_url": download_url(tag, path.name)}
        for path in files if path.name not in omit]}


def as_bytes(value):
    return json.dumps(value).encode("utf-8")


class PublicGitHub:
    """Unauthenticated reads of the destination, served from a table of URL to bytes. Any other URL is a 404."""

    def __init__(self, table):
        self.table = table
        self.urls = []

    def __call__(self, url, limit):
        self.urls.append(url)
        if url not in self.table:
            raise RuntimeError(f"HTTP Error 404: {url}")
        return self.table[url]


class GenerationFixtureTests(unittest.TestCase):
    def test_this_release_packs_generation_one_plus_the_arc_pair(self):
        # The packer carries the ARC pair, so an archive this release packs extracts under generation 2 and not under generation 1.
        self.assertEqual(WINDOWS_RESOURCE_FILES, GEN1_RESOURCE_FILES | frozenset(ARC_FILES))
        self.assertEqual(WINDOWS_RESOURCE_TREES, GEN1_RESOURCE_TREES)

    def test_generation_two_requires_exactly_the_two_arc_files_on_top_of_generation_one(self):
        self.assertEqual(GEN2_ARC_FILES, frozenset(ARC_FILES))
        self.assertEqual(GENERATIONS[2]["required"] - GENERATIONS[1]["required"], frozenset(ARC_FILES))
        self.assertEqual(GENERATIONS[2]["files"], GENERATIONS[2]["required"])
        self.assertEqual(GENERATIONS[2]["trees"], GENERATIONS[1]["trees"])

    def test_the_packer_refuses_a_release_without_the_arc_pair(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_build(root, NEW)
            (root / ARC_FILES[0]).unlink()
            with self.assertRaisesRegex(ValueError, "missing a required Companion MCP resource"):
                package_windows(root, NEW)


class ArchiveEntryTests(unittest.TestCase):
    def test_a_release_archive_from_the_packer_extracts_under_generation_two_and_not_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_build(root, NEW)
            names = archive_names(package_windows(root, NEW))
            self.assertEqual(generation_problems(names, NEW, 2), [])
            self.assertEqual(sorted(generation_problems(names, NEW, 1)),
                             sorted(f"unexpected entry {name}" for name in ARC_FILES))

    def test_an_archive_without_the_arc_pair_extracts_under_generation_one_and_not_two(self):
        # The builder must never point generation 2 at such an archive: the updater that reads generation 2 refuses it.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_build(root, NEW)
            names = archive_names(archive_of(root, NEW, omit=ARC_FILES))
            self.assertEqual(generation_problems(names, NEW, 1), [])
            self.assertEqual(sorted(generation_problems(names, NEW, 2)), sorted(f"missing {name}" for name in ARC_FILES))

    def test_an_archive_missing_a_required_resource_extracts_under_neither_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_build(root, NEW)
            names = archive_names(archive_of(root, NEW, omit=("resources/arc_desktop/arc-desktop.exe",)))
            for generation in (1, 2):
                with self.subTest(generation=generation):
                    self.assertIn("missing resources/arc_desktop/arc-desktop.exe", generation_problems(names, NEW, generation))


class PointerTests(unittest.TestCase):
    def test_a_release_with_the_arc_pair_names_itself_for_generation_two_and_copies_generation_one(self):
        with tempfile.TemporaryDirectory() as directory:
            older_g1 = (stage_release(Path(directory) / "older", OLD, OLD_SHA, generation=1) / G1).read_bytes()
            folder = stage_release(Path(directory) / "new", NEW, NEW_SHA, previous=from_older(older_g1))
            self.assertEqual((folder / G1).read_bytes(), older_g1)
            self.assertEqual(json.loads((folder / G2).read_bytes())["tag"], NEW)

    def test_a_release_without_the_arc_pair_names_itself_for_generation_one_and_copies_generation_two(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = stage_release(Path(directory), NEW, NEW_SHA, generation=1)
            self.assertEqual(json.loads((folder / G1).read_bytes())["tag"], NEW)
            self.assertEqual((folder / G2).read_bytes(), pointer_to(OLDEST, OLDEST_SHA))

    def test_a_0195_shaped_release_copies_the_0194_generation_one_receipt_and_names_0195_for_generation_two(self):
        # The release the owner ships: its archive carries the ARC pair, so generation 1 refuses it. Its generation-1 receipt is the
        # v0.19.4 stepping stone's, byte for byte, and its generation-2 receipt names v0.19.5 and no other release.
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            stepping = stage_release(base / "0194", "v0.19.4", "1" * 40, generation=1)
            g1_0194 = (stepping / G1).read_bytes()
            folder = stage_release(base / "0195", "v0.19.5", "2" * 40, previous=from_older(g1_0194))
            self.assertEqual((folder / G1).read_bytes(), g1_0194)
            own = json.loads((folder / G2).read_bytes())
            self.assertEqual((own["tag"], own["version"], own["source_sha"]), ("v0.19.5", "0.19.5", "2" * 40))
            receipt, _ = inspect(folder, "v0.19.5", "2" * 40, "windows-amd64")
            self.assertEqual(receipt["tag"], "v0.19.4", "the generation-1 receipt is the stepping stone")
            names = archive_names(folder / filenames("v0.19.5", "windows-amd64")[0])
            self.assertEqual(generation_problems(names, "v0.19.5", 2), [])
            self.assertEqual(sorted(generation_problems(names, "v0.19.5", 1)),
                             sorted(f"unexpected entry {name}" for name in ARC_FILES))

    def test_an_archive_carrying_the_arc_pair_must_not_move_the_generation_one_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            older_g1 = (stage_release(Path(directory) / "older", OLD, OLD_SHA, generation=1) / G1).read_bytes()
            calls = []

            def previous(name):
                calls.append(name)
                return older_g1 if name == G1 else None

            receipts = windows_receipts(arc_archive(Path(directory) / "arc", NEW), NEW, OWN, previous)
            self.assertEqual(receipts[G1], older_g1, "the generation-1 pointer is the older release's receipt, byte for byte")
            self.assertEqual(receipts[G2], OWN, "generation 2 requires the ARC files, so it names this release")
            self.assertEqual(calls, [G1], "only the generation the archive misses asks for a copy")

    def test_without_a_published_copy_a_generation_the_archive_misses_refuses_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = arc_archive(directory, NEW)
            with self.assertRaisesRegex(ValueError, f"generation 1.*no valid {G1}.*refusing to publish"):
                windows_receipts(archive, NEW, OWN, previous=lambda name: None)
            with self.assertRaisesRegex(ValueError, "refusing to publish"):
                windows_receipts(archive, NEW, OWN, previous=None)

    def test_a_copied_receipt_that_names_this_release_or_is_not_self_consistent_is_never_used(self):
        with tempfile.TemporaryDirectory() as directory:
            older = json.loads((stage_release(Path(directory) / "older", OLD, OLD_SHA, generation=1) / G1).read_bytes())
            archive = arc_archive(Path(directory) / "arc", NEW)
            cases = {
                "names this release": (as_bytes({**older, "tag": NEW}), "does not name an older release"),
                "not JSON": (b"not a receipt", "not JSON"),
                "version disagrees with its tag": (as_bytes({**older, "version": "9.9.9"}), "not self-consistent"),
                "an asset is missing": (as_bytes({**older, "assets": {
                    name: value for name, value in older["assets"].items() if name != CHECKSUM}}), "not self-consistent"),
                "signed as another platform": (as_bytes({**older, "platform": "darwin-arm64"}), "not self-consistent"),
            }
            for label, (copy, message) in cases.items():
                with self.subTest(case=label), self.assertRaisesRegex(ValueError, message):
                    windows_receipts(archive, NEW, OWN, previous=lambda name, copy=copy: copy)

    def test_prepare_copies_the_older_generation_one_receipt_and_inspect_accepts_the_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            older_g1 = (stage_release(Path(directory) / "older", OLD, OLD_SHA, generation=1) / G1).read_bytes()
            folder = stage_release(Path(directory) / "new", NEW, NEW_SHA, previous=from_older(older_g1))
            self.assertEqual((folder / G1).read_bytes(), older_g1)
            self.assertEqual(json.loads((folder / G2).read_bytes())["tag"], NEW)
            receipt, files = inspect(folder, NEW, NEW_SHA, "windows-amd64")
            self.assertEqual(receipt["tag"], OLD, "the generation-1 receipt is the pointer")
            self.assertEqual({path.name for path in files}, {filenames(NEW, "windows-amd64")[0], CHECKSUM, G1, G2})


class BindingTests(unittest.TestCase):
    def test_inspect_refuses_a_generation_one_receipt_that_claims_this_release_when_its_archive_carries_an_arc_file(self):
        with tempfile.TemporaryDirectory() as directory:
            older_g1 = (stage_release(Path(directory) / "older", OLD, OLD_SHA, generation=1) / G1).read_bytes()
            folder = stage_release(Path(directory) / "new", NEW, NEW_SHA, previous=from_older(older_g1))
            (folder / G1).write_bytes((folder / G2).read_bytes())
            with self.assertRaisesRegex(ValueError, "does not name an older release"):
                inspect(folder, NEW, NEW_SHA, "windows-amd64")


class NewestPublishedCopyTests(unittest.TestCase):
    """The stage-time fetch: the newest published release's receipt, verified against GitHub's own record of it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.older_folder = stage_release(base / "older", OLD, OLD_SHA, generation=1)
        self.older_files = files_of(self.older_folder)
        self.older_g1 = (self.older_folder / G1).read_bytes()

    def fetch(self, releases, extra=None):
        table = {LISTING: as_bytes(releases), tags_url(OLD): as_bytes(release_json(OLD, self.older_files))}
        table[download_url(OLD, G1)] = self.older_g1
        table.update(extra or {})
        github = PublicGitHub(table)
        return github, lambda: previous_receipt(REPO, G1, "windows-amd64", NEW, read=github)

    def test_the_copy_is_the_newest_published_receipt_byte_for_byte(self):
        github, fetch = self.fetch([release_json(OLD, self.older_files)])
        self.assertEqual(fetch(), self.older_g1)
        self.assertEqual(github.urls[0], LISTING)

    def test_a_draft_is_never_the_source_of_a_copy(self):
        draft_files = files_of(stage_release(Path(self.directory.name) / "draft", "v1.2.4", "e" * 40, generation=1))
        github, fetch = self.fetch([release_json("v1.2.4", draft_files, draft=True), release_json(OLD, self.older_files)])
        self.assertEqual(fetch(), self.older_g1)
        self.assertNotIn(download_url("v1.2.4", G1), github.urls)

    def test_a_newest_release_without_the_generation_receipt_gives_no_copy_and_no_walk_back(self):
        older_still = files_of(stage_release(Path(self.directory.name) / "still", OLDER, OLDER_SHA, generation=1))
        github, fetch = self.fetch([release_json(OLD, self.older_files, omit=(G1,)), release_json(OLDER, older_still)])
        self.assertIsNone(fetch(), "an older release's receipt must not stand in for a newest one that lacks it")
        self.assertEqual(github.urls, [LISTING])

    def test_a_copy_is_refused_when_its_digest_disagrees_with_github(self):
        release = release_json(OLD, self.older_files)
        next(asset for asset in release["assets"] if asset["name"] == G1)["digest"] = "sha256:" + "0" * 64
        _, fetch = self.fetch([release])
        with self.assertRaisesRegex(ValueError, "does not match GitHub's digest"):
            fetch()

    def test_a_copy_is_refused_when_the_release_it_names_no_longer_lists_its_archive_and_checksum(self):
        named = release_json(OLD, self.older_files, omit=(filenames(OLD, "windows-amd64")[0],))
        _, fetch = self.fetch([release_json(OLD, self.older_files)], extra={tags_url(OLD): as_bytes(named)})
        with self.assertRaisesRegex(ValueError, "no longer lists its archive and checksum"):
            fetch()

    def test_a_copy_that_names_an_unpublished_release_is_refused(self):
        table_without_tag = {LISTING: as_bytes([release_json(OLD, self.older_files)]), download_url(OLD, G1): self.older_g1}
        github = PublicGitHub(table_without_tag)
        with self.assertRaisesRegex(RuntimeError, "404"):
            previous_receipt(REPO, G1, "windows-amd64", NEW, read=github)

    def test_a_copy_of_a_copy_is_checked_against_the_release_that_holds_the_bytes(self):
        older_folder = stage_release(Path(self.directory.name) / "v121", OLDER, OLDER_SHA, generation=1)
        older_bytes = (older_folder / G1).read_bytes()
        chained = stage_release(Path(self.directory.name) / "chain", OLD, OLD_SHA, previous=from_older(older_bytes))
        chained_files = files_of(chained)
        chained_g1 = (chained / G1).read_bytes()
        table = {
            LISTING: as_bytes([release_json(OLD, chained_files)]),
            download_url(OLD, G1): chained_g1,
            tags_url(OLDER): as_bytes(release_json(OLDER, files_of(older_folder))),
        }
        github = PublicGitHub(table)
        self.assertEqual(previous_receipt(REPO, G1, "windows-amd64", NEW, read=github), chained_g1)
        self.assertIn(tags_url(OLDER), github.urls)

    def test_a_destination_that_is_not_an_aios_companion_repository_is_refused(self):
        with self.assertRaisesRegex(ValueError, "not set"):
            previous_receipt("", G1, "windows-amd64", NEW, read=PublicGitHub({}))
        with self.assertRaisesRegex(ValueError, "not set"):
            previous_receipt("someone/else", G1, "windows-amd64", NEW, read=PublicGitHub({}))


class FinalizeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.older_folder = stage_release(base / "older", OLD, OLD_SHA, generation=1)
        self.older_files = files_of(self.older_folder)
        self.new_folder = stage_release(base / "new", NEW, NEW_SHA, previous=from_older((self.older_folder / G1).read_bytes()))
        darwin_root = base / "darwin"
        darwin_root.mkdir()
        (darwin_root / filenames(NEW, "darwin-arm64")[0]).write_bytes(b"synthetic darwin")
        prepare(darwin_root, base / "darwin-assets", NEW, NEW_SHA, "darwin-arm64")
        self.darwin_folder = base / "darwin-assets"

    def run_finalize(self, files, named_release):
        assets, downloads = [], {}
        for path in files:
            number = len(assets) + 1
            assets.append(dict(id=number, name=path.name, size=path.stat().st_size, digest="sha256:" + digest(path)))
            downloads[str(number)] = path.read_bytes()
        release = dict(id=77, assets=assets, body=marker(NEW, NEW_SHA, False))

        def gh(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=downloads[args[-1].rsplit("/", 1)[-1]])

        def github_api(method, path, payload=None):
            if method == "GET" and path == f"repos/{REPO}/releases/tags/{OLD}":
                return named_release
            if method == "PATCH":
                return {"id": 77, "draft": True, "tag_name": NEW}
            raise AssertionError((method, path))

        with patch("app_release_publish.draft", return_value=release), \
                patch("app_release_publish.subprocess.run", side_effect=gh), \
                patch("app_release_publish.api", side_effect=github_api) as api:
            finalize(REPO, NEW, NEW_SHA)
        return api

    def windows_and_darwin(self, windows_files):
        return [*windows_files, *files_of(self.darwin_folder)]

    def test_finalize_accepts_a_copied_generation_one_pointer_while_its_named_release_is_published(self):
        api = self.run_finalize(self.windows_and_darwin(files_of(self.new_folder)), release_json(OLD, self.older_files))
        methods = [call.args[:2] for call in api.call_args_list]
        self.assertIn(("GET", f"repos/{REPO}/releases/tags/{OLD}"), methods)
        self.assertEqual(methods[-1], ("PATCH", f"repos/{REPO}/releases/77"))

    def test_finalize_refuses_a_copied_pointer_whose_named_release_lost_its_archive(self):
        named = release_json(OLD, self.older_files, omit=(filenames(OLD, "windows-amd64")[0],))
        with self.assertRaisesRegex(ValueError, "no longer lists its archive and checksum"):
            self.run_finalize(self.windows_and_darwin(files_of(self.new_folder)), named)

    def test_finalize_accepts_a_release_that_names_itself_for_generation_one_and_copies_generation_two(self):
        # A release without the ARC pair extracts under generation 1 only: it names itself there and copies generation 2 from the newest
        # published release that holds one. The copy's named release is the one GitHub lists for it.
        copy = pointer_to(OLD, OLD_SHA)
        stepping = stage_release(Path(self.directory.name) / "stepping", NEW, NEW_SHA, generation=1,
                                 previous=lambda name: copy if name == G2 else None)
        api = self.run_finalize(self.windows_and_darwin(files_of(stepping)), pointer_listing(copy))
        methods = [call.args[:2] for call in api.call_args_list]
        self.assertIn(("GET", f"repos/{REPO}/releases/tags/{OLD}"), methods)
        self.assertEqual(methods[-1], ("PATCH", f"repos/{REPO}/releases/77"))


if __name__ == "__main__":
    unittest.main()
