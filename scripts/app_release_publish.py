"""Only draft creation/asset publication use the new app's publishing credential.

These steps run inside run_sealed.py: GitHub's error messages go into the encrypted log, never the public one.

A draft is found through the releases listing, never through GET /releases/tags/{tag}: that endpoint returns
published releases only (it answers 404 for a draft, checked against a real draft on 17 Nov 2026), so a lookup
there cannot find the draft this workflow created and would create a second one on every dispatch. This mirrors
how today's releases are found (aios-desktop-app scripts/release_metadata.py) and how gh itself finds drafts.
"""
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote

from app_release_identity import validate_destination
from app_release_artifacts import SHA, digest, filenames, inspect, version

# A listing that reaches this many pages could hide a published release on a later page: a refusal, never an answer.
LISTING_PAGES = 10
PAGE_SIZE = 100


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
        expected.update((f"SHA256SUMS-{platform}.txt", f"companion-release-{platform}.json"))
    if len(names) != len(set(names)) or set(names) != expected:
        raise ValueError("Both complete platform asset sets are required before finalizing the draft")
    by_name = {asset["name"]: asset for asset in assets}
    for platform in ("windows-amd64", "darwin-arm64"):
        name = f"companion-release-{platform}.json"
        asset = by_name[name]
        if not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= 65536:
            raise ValueError("Invalid platform receipt size")
        raw = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
            f"repos/{repository}/releases/assets/{asset['id']}"], capture_output=True, check=False)
        if raw.returncode or len(raw.stdout) > 65536:
            raise RuntimeError("Platform receipt could not be read")
        receipt = json.loads(raw.stdout)
        expected_signing = "unsigned-test" if platform == "darwin-arm64" and unsigned else (
            "signed-notarized" if platform == "darwin-arm64" else "unsigned-windows")
        hashes = receipt.get("assets", {})
        expected_files = set(filenames(tag, platform, unsigned)) | {f"SHA256SUMS-{platform}.txt"}
        if (receipt.get("schema") != 1 or receipt.get("source_sha") != source_sha
                or receipt.get("tag") != tag or receipt.get("version") != version(tag)
                or receipt.get("platform") != platform or receipt.get("signing") != expected_signing
                or set(hashes) != expected_files):
            raise ValueError("Published platform receipt has a different source or signing mode")
        for file, hash in hashes.items():
            if by_name[file].get("digest") != "sha256:" + hash:
                raise ValueError("Published draft bytes do not match their platform receipt")
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
