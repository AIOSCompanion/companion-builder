"""Only draft creation/asset publication use the new app's publishing credential.

These steps run inside run_sealed.py: GitHub's error messages go into the encrypted log, never the public one.

A draft is found through the releases listing, never through GET /releases/tags/{tag}: that endpoint returns
published releases only (it answers 404 for a draft, checked against a real draft on 17 Nov 2026), so a lookup
there cannot find the draft this workflow created and would create a second one on every dispatch. This mirrors
how today's releases are found (aios-desktop-app scripts/release_metadata.py) and how gh itself finds drafts.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

from app_release_identity import validate_destination
from app_release_artifacts import SHA, digest, filenames, inspect, receipt_names, signing_for, validate_pointer, version

# A listing that reaches this many pages could hide a published release on a later page: a refusal, never an answer.
LISTING_PAGES = 10
PAGE_SIZE = 100
REPOSITORY = re.compile(r"AIOSCompanion/[A-Za-z0-9._-]+")
RECEIPT_LIMIT = 65536
METADATA_LIMIT = 4 << 20


def api(method, path, payload=None):
    args = ["gh", "api", "--method", method, path]
    if payload is not None:
        args += ["--input", "-"]
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()[:400]
        raise RuntimeError(f"GitHub API {method} {path.split('?', 1)[0]} failed (exit {result.returncode}): {detail}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def releases_for(repository, tag, request):
    """Every release in the destination carrying this tag, drafts included (the credential can push)."""
    names = {tag, version(tag)}
    found = []
    for page in range(1, LISTING_PAGES + 1):
        batch = request("GET", f"repos/{repository}/releases?per_page={PAGE_SIZE}&page={page}")
        if not isinstance(batch, list) or not all(isinstance(item, dict) for item in batch):
            raise ValueError("Release listing is malformed")
        found.extend(item for item in batch if item.get("tag_name") in names)
        if len(batch) < PAGE_SIZE:
            return found
    raise ValueError("Release listing reached its bound; a published release could be hidden")


def single_draft(matches):
    if any(item.get("draft") is not True for item in matches):
        raise ValueError("A published release already carries this tag; cut a new version tag instead")
    if len(matches) > 1:
        raise ValueError("More than one draft carries this tag; delete the extra drafts first")
    return matches[0] if matches else None


def marker(tag, source_sha, unsigned):
    version(tag)
    if not SHA.fullmatch(source_sha or ""):
        raise ValueError("Invalid immutable source commit")
    return "<!-- companion-app-release " + json.dumps({"source_sha": source_sha, "tag": tag,
        "unsigned_macos_test": unsigned}, sort_keys=True, separators=(",", ":")) + " -->"


def draft(repository, tag, source_sha, unsigned=False, *, create=False, request=api):
    metadata = validate_destination(repository, lambda path: request("GET", path))
    expected = marker(tag, source_sha, unsigned)
    found = single_draft(releases_for(repository, tag, request))
    if found is None:
        if not create:
            raise ValueError("The draft for this tag is missing; dispatch the release again to create it")
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError("Public releases repository must first be initialized by its owner")
        commit = request("GET", f"repos/{repository}/git/ref/heads/{quote(branch, safe='')}")["object"]
        if commit.get("type") != "commit":
            raise ValueError("Public release destination has no initial commit")
        body = "Companion Go/Wails app.\n\n" + ("**UNSIGNED macOS TEST BUILD.**\n\n" if unsigned else "") + expected
        created = request("POST", f"repos/{repository}/releases", {"tag_name": tag,
            "target_commitish": commit["sha"], "name": f"Companion {version(tag)}" + (" - UNSIGNED macOS TEST" if unsigned else ""),
            "draft": True, "prerelease": unsigned or "-" in version(tag), "body": body,
            "generate_release_notes": False})
        if not isinstance(created, dict) or type(created.get("id")) is not int:
            raise ValueError("GitHub did not return the new draft identity")
        # GitHub can acknowledge creation before the draft appears in the list.
        # Re-read with a bound and still refuse a competing or published release.
        for delay in (0, 1, 2, 4, 8):
            if delay:
                time.sleep(delay)
            found = single_draft(releases_for(repository, tag, request))
            if found is not None:
                if found.get("id") != created["id"]:
                    raise ValueError("A different draft appeared after creation")
                break
        if found is None:
            raise ValueError("The created draft is not listed")
    if not isinstance(found.get("id"), int):
        raise ValueError("Invalid draft release identity")
    # The release by its immutable ID, read fresh: its body, flags and assets.
    release = request("GET", f"repos/{repository}/releases/{found['id']}")
    if (not release or release.get("draft") is not True or release.get("tag_name") != tag
            or re.findall(r"<!-- companion-app-release .*? -->", release.get("body") or "") != [expected]
            or (unsigned and release.get("prerelease") is not True)):
        raise ValueError("Release is missing, published, or belongs to a different source/mode")
    return release


def read_public(url, limit):
    """An unauthenticated GET of a public GitHub URL. The build job holds no credential for the destination, and needs none."""
    accept = "application/vnd.github+json" if url.startswith("https://api.github.com/") else "application/octet-stream"
    with urlopen(Request(url, headers={"Accept": accept, "User-Agent": "companion-builder"}), timeout=60) as response:
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Public release metadata is larger than the builder accepts")
    return body


def named_release_holds(release, receipt):
    """Whether GitHub's record of the release a copied receipt names is published and still lists that release's archive
    and checksum with the receipt's hashes."""
    if not isinstance(release, dict) or release.get("draft") is not False or release.get("tag_name") != receipt["tag"]:
        return False
    listed = {asset.get("name"): asset.get("digest") for asset in release.get("assets") or [] if isinstance(asset, dict)}
    return all(listed.get(file) == "sha256:" + value for file, value in receipt["assets"].items())


def previous_receipt(repository, name, platform, tag, read=read_public):
    """The receipt `name` of the newest published release in the destination, as its exact bytes; None when that release
    has no such receipt.

    GitHub lists releases newest first, and the unauthenticated listing hides drafts, so the first entry is the newest
    published release. Its receipt is taken only when it is a valid pointer whose named release is still published and
    still lists the archive and checksum with the receipt's hashes.
    """
    if not REPOSITORY.fullmatch(repository or ""):
        raise ValueError("The destination repository is not set for this release")
    releases = json.loads(read(f"https://api.github.com/repos/{repository}/releases?per_page=10", METADATA_LIMIT))
    if not isinstance(releases, list):
        raise ValueError("Release listing is malformed")
    latest = next((item for item in releases if isinstance(item, dict) and item.get("draft") is False), None)
    if latest is None:
        return None
    asset = next((item for item in latest.get("assets") or [] if isinstance(item, dict) and item.get("name") == name), None)
    if asset is None:
        return None
    if not isinstance(asset.get("digest"), str) or not asset["digest"].startswith("sha256:") \
            or not isinstance(asset.get("browser_download_url"), str):
        raise ValueError("GitHub did not report a digest and download address for the newest receipt")
    raw = read(asset["browser_download_url"], RECEIPT_LIMIT)
    if "sha256:" + hashlib.sha256(raw).hexdigest() != asset["digest"]:
        raise ValueError("The newest published receipt does not match GitHub's digest for it")
    receipt = validate_pointer(raw, platform, tag)
    named = json.loads(read(f"https://api.github.com/repos/{repository}/releases/tags/{receipt['tag']}", METADATA_LIMIT))
    if not named_release_holds(named, receipt):
        raise ValueError(f"The release that {name} names is not published or no longer lists its archive and checksum")
    return raw


def upload(repository, tag, source_sha, folder, platform, unsigned=False):
    _, files = inspect(folder, tag, source_sha, platform, unsigned)
    release = draft(repository, tag, source_sha, unsigned)
    existing = {asset["name"]: asset for asset in release.get("assets", [])}
    for file in files:
        asset = existing.get(file.name)
        if asset:
            # Never clobber a draft asset from another attempt. Current GitHub
            # supplies digests; when absent, download privately for comparison.
            known = asset.get("digest")
            if not known:
                with tempfile.TemporaryFile() as output:
                    result = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                        f"repos/{repository}/releases/assets/{asset['id']}"], stdout=output, stderr=subprocess.PIPE)
                    if result.returncode:
                        raise RuntimeError("Existing draft asset could not be verified")
                    output.seek(0)
                    import hashlib
                    known = "sha256:" + hashlib.file_digest(output, "sha256").hexdigest()
            if known != "sha256:" + digest(file):
                raise ValueError("Draft already has different bytes under this asset name")
            continue
        # Recheck immediately before each mutation. Owners must keep the draft
        # unpublished until finalization: GitHub upload has no atomic draft-only
        # condition, so this check cannot exclude a concurrent owner publication.
        draft(repository, tag, source_sha, unsigned)
        result = subprocess.run(["gh", "release", "upload", tag, str(file), "--repo", repository],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[:400]
            raise RuntimeError(f"Draft asset upload of {file.name} failed (exit {result.returncode}): {detail}")
    print(f"Verified {len(files)} allowlisted assets on the isolated unpublished draft")


def finalize(repository, tag, source_sha, unsigned=False):
    release = draft(repository, tag, source_sha, unsigned)
    assets = release.get("assets", [])
    names = [asset["name"] for asset in assets]
    expected = set()
    for platform in ("windows-amd64", "darwin-arm64"):
        expected.update(filenames(tag, platform, unsigned))
        expected.update((f"SHA256SUMS-{platform}.txt", *receipt_names(platform)))
    if len(names) != len(set(names)) or set(names) != expected:
        raise ValueError("Both complete platform asset sets are required before finalizing the draft")
    by_name = {asset["name"]: asset for asset in assets}
    own = {}
    for platform in ("windows-amd64", "darwin-arm64"):
        for name in receipt_names(platform):
            asset = by_name[name]
            if not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= 65536:
                raise ValueError("Invalid platform receipt size")
            raw = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                f"repos/{repository}/releases/assets/{asset['id']}"], capture_output=True, check=False)
            if raw.returncode or len(raw.stdout) > 65536:
                raise RuntimeError("Platform receipt could not be read")
            receipt = json.loads(raw.stdout)
            if platform == "windows-amd64" and receipt.get("tag") != tag:
                # A copied pointer: it must name a release GitHub still lists as published, with the same archive and checksum.
                own[name] = False
                receipt = validate_pointer(raw.stdout, platform, tag)
                named = api("GET", f"repos/{repository}/releases/tags/{receipt['tag']}")
                if not named_release_holds(named, receipt):
                    raise ValueError(f"The release that {name} names is not published or no longer lists its archive and checksum")
                continue
            own[name] = True
            hashes = receipt.get("assets", {})
            expected_files = set(filenames(tag, platform, unsigned)) | {f"SHA256SUMS-{platform}.txt"}
            if (receipt.get("schema") != 1 or receipt.get("source_sha") != source_sha
                    or receipt.get("tag") != tag or receipt.get("version") != version(tag)
                    or receipt.get("platform") != platform or receipt.get("signing") != signing_for(platform, unsigned)
                    or set(hashes) != expected_files):
                raise ValueError("Published platform receipt has a different source or signing mode")
            for file, hash in hashes.items():
                if by_name[file].get("digest") != "sha256:" + hash:
                    raise ValueError("Published draft bytes do not match their platform receipt")
    # Generations are not nested: generation 2 requires the ARC files that generation 1 refuses, so a release names itself for
    # the generations its archive extracts under, and a copy for the others. A release that names itself for neither is not built.
    generation_one, generation_two = receipt_names("windows-amd64")
    if not own[generation_one] and not own[generation_two]:
        raise ValueError("No Windows platform receipt names this release")
    # This is a publication step but never publishes the draft. It marks the
    # complete validated asset set while explicitly retaining owner publication.
    checked_release = draft(repository, tag, source_sha, unsigned)
    body = (checked_release.get("body") or "").split("\n\n<!-- companion-app-verified", 1)[0]
    body += "\n\n<!-- companion-app-verified: windows-package-checks; " + (
        "unsigned-macos-test" if unsigned else "signed-notarized-stapled-macos") + " -->"
    updated = api("PATCH", f"repos/{repository}/releases/{checked_release['id']}", {
        "body": body, "draft": True, "tag_name": tag, "prerelease": unsigned or "-" in version(tag),
    })
    if (not isinstance(updated, dict) or updated.get("id") != checked_release["id"]
            or updated.get("draft") is not True or updated.get("tag_name") != tag):
        raise ValueError("GitHub did not retain the identity of the finalized draft")
    print("Both platform receipts and draft assets verified; release remains unpublished")
