"""The bridge installer: built only from the verified Go release, drafted (never published) in the Electron updater's repository.

The Go app's Windows archive and its receipt are fetched from the app's release draft (the publishing credential reads it), then
checked against the release's own checksum list before any private source is present. The private engine is checked out at the
exact tag, and scripts/package-bridge.ps1 builds the installer from that archive and receipt: the engine's bridge-pack verifies the
receipt and extracts the payload with the updater's own policy. This module then checks the three outputs against each other and
against the electron-updater format, and the draft is created with --draft and verified as a draft. Nothing is published, tagged
or made the latest release.

The destination is named here, not read from a variable: the Go app's pipeline refuses this repository (app_release_identity), so
only this module can address it, and only as a draft.

Commands (each runs inside run_sealed.py; the cause goes to the sealed log, never to the public one):
    bridge_release.py fetch   TAG APP_RELEASE_REPOSITORY  -> BRIDGE_INPUT_DIR (archive, receipt, checksum list)
    bridge_release.py build                              -> BRIDGE_OUT_DIR (installer, blockmap, latest.yml)
    bridge_release.py draft                              -> a draft release in BRIDGE_DESTINATION
"""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_release_publish import api, releases_for, single_draft
from app_release_artifacts import RECEIPTS, digest, version as tag_version

BRIDGE_DESTINATION = "AIOSCompanion/aios-companion-releases"
GO_PLATFORM = "windows-amd64"
GO_ARCHIVE = "Companion_{v}_windows_amd64.zip"
GO_CHECKSUMS = "SHA256SUMS-windows-amd64.txt"
BRIDGE_ASSETS = ("Companion-Setup-x64-{v}.exe", "Companion-Setup-x64-{v}.exe.blockmap", "latest.yml")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
REPOSITORY = re.compile(r"AIOSCompanion/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
NOTES = ("Bridge installer for the Go app, drafted by the release builder. Not published: the owner publishes it, and the "
         "electron-updater feed only serves a published release.")


def bridge_version(tag):
    """The Go app's tag as the bridge version. Only a plain x.y.z is a bridge release; a pre-release tag is refused."""
    value = tag_version(tag)
    if not VERSION.fullmatch(value):
        raise ValueError("The bridge is built only for a plain x.y.z release tag")
    return value


def expected_names(value, names):
    return [name.format(v=value) for name in names]


def _raw_download(repository, asset_id, path):
    """The asset's bytes, streamed to path with the publishing credential (the API serves drafts' assets to it)."""
    with open(path, "wb") as target:
        result = subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                                 f"repos/{repository}/releases/assets/{asset_id}"],
                                stdout=target, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"asset download failed with exit {result.returncode}")


def receipt_candidates():
    """The Windows receipt names, highest generation first. A release publishes one per generation. The bridge takes the first
    of these that names the release it is built from (select_receipt), so a newer release's older-generation copy never wins."""
    return [RECEIPTS[GO_PLATFORM][generation] for generation in sorted(RECEIPTS[GO_PLATFORM], reverse=True)]


def select_receipt(folder, tag):
    """The receipt in folder that names this release by its tag and version, checked from the highest generation down. A receipt
    that does not read is refused, and a release that no receipt names is refused. Receipts of other releases never match."""
    value = bridge_version(tag)
    for name in receipt_candidates():
        path = Path(folder) / name
        if not path.is_file():
            continue
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError(f"{name} is not a readable receipt") from None
        if isinstance(receipt, dict) and receipt.get("tag") == "v" + value and receipt.get("version") == value:
            return name
    raise ValueError(f"No Go release receipt names {tag}")


def fetch(tag, repository, out, request=api, download=_raw_download):
    """Downloads the Go release's Windows archive, its checksum list and every receipt it publishes, and checks each against
    GitHub's digest. The archive is checked against the checksum list. Returns [archive, the receipt that names this release,
    checksum list]; a release that no receipt names is refused."""
    value = bridge_version(tag)
    if not REPOSITORY.fullmatch(repository or ""):
        raise ValueError("The Go release repository is not configured")
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        raise ValueError("The bridge input folder must be empty")
    out.mkdir(parents=True, exist_ok=True)
    release = single_draft_or_published(repository, tag, request)
    assets = {item.get("name"): item for item in release.get("assets", []) if isinstance(item, dict)}
    archive, checksums = expected_names(value, (GO_ARCHIVE, GO_CHECKSUMS))
    for name in (archive, checksums):
        if name not in assets:
            raise ValueError(f"The Go release has no {name}")
    receipts = [name for name in receipt_candidates() if name in assets]
    for name in [archive, checksums, *receipts]:
        download(repository, assets[name]["id"], out / name)
        if assets[name].get("digest") != "sha256:" + digest(out / name):
            raise ValueError(f"{name} does not match the digest GitHub recorded for it")
    listed = {}
    for line in (out / checksums).read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^/\\]+)", line)
        if match:
            listed[match[2]] = match[1].lower()
    if listed.get(archive) != digest(out / archive):
        raise ValueError("The archive does not match the Go release's checksum list")
    return [archive, select_receipt(out, tag), checksums]


def single_draft_or_published(repository, tag, request):
    """The one release (draft or published) that carries the Go tag. A second match is refused."""
    matches = releases_for(repository, tag, request)
    if len(matches) != 1:
        raise ValueError("The Go release must be exactly one release for this tag")
    return matches[0]


def sha512_b64(path):
    return base64.b64encode(hashlib.sha512(Path(path).read_bytes()).digest()).decode("ascii")


def parse_latest(text):
    """The electron-updater feed the bridge writes: a version, one file (url, sha512, size), a path, a sha512 and a date."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    fields = {}
    files = []
    for line in lines:
        if line == "files:":
            continue
        if line.startswith("  - url: "):
            files.append({"url": line[len("  - url: "):]})
        elif line.startswith("    ") and files and ":" in line:
            key, _, value = line.strip().partition(": ")
            files[-1][key] = value
        else:
            key, _, value = line.partition(": ")
            fields[key] = value
    if len(files) != 1 or set(fields) != {"version", "path", "sha512", "releaseDate"}:
        raise ValueError("latest.yml does not have the electron-updater shape")
    return {"version": fields["version"], "path": fields["path"], "sha512": fields["sha512"],
            "releaseDate": fields["releaseDate"], "file": files[0]}


def verify_outputs(out, value):
    """The three outputs agree with each other and with electron-updater's format; any other file is refused."""
    out = Path(out)
    names = expected_names(value, BRIDGE_ASSETS)
    present = sorted(p.name for p in out.iterdir())
    if present != sorted(names):
        raise ValueError(f"The bridge output must be exactly {sorted(names)}, not {present}")
    installer = out / names[0]
    if installer.stat().st_size == 0:
        raise ValueError("The installer is empty")
    feed = parse_latest((out / "latest.yml").read_text(encoding="utf-8"))
    if feed["version"] != value or feed["path"] != names[0] or feed["file"]["url"] != names[0]:
        raise ValueError("latest.yml names another version or file")
    if feed["sha512"] != sha512_b64(installer) or feed["file"]["sha512"] != feed["sha512"]:
        raise ValueError("latest.yml does not carry the installer's sha512")
    if feed["file"].get("size") != str(installer.stat().st_size):
        raise ValueError("latest.yml does not carry the installer's size")
    blockmap = json.loads(gzip.decompress((out / names[1]).read_bytes()).decode("utf-8"))
    if str(blockmap.get("version")) != "2":
        raise ValueError("The blockmap is not the electron-updater format")
    if not blockmap.get("files") or blockmap["files"][0].get("name") != "file":
        raise ValueError("The blockmap does not describe the installer")
    return {name: out / name for name in names}


def _child_env(env):
    """The build's environment: no publishing or source credential reaches the private build."""
    keep = {key: value for key, value in env.items() if "TOKEN" not in key.upper()}
    keep.update(GOTOOLCHAIN="local", GIT_TERMINAL_PROMPT="0")
    return keep


def build(env, run=subprocess.run):
    """Runs the engine's package-bridge.ps1 on the verified archive, from the private checkout at the exact tag."""
    from app_release import source_identity
    tag = env.get("TAG", "")
    value = bridge_version(tag)
    if env.get("GH_TOKEN") or env.get("PUBLISH_RELEASES_TOKEN"):
        raise ValueError("The build must not hold a credential")
    source, _, _ = source_identity(env)
    inp = Path(env["BRIDGE_INPUT_DIR"]).resolve(strict=True)
    archive = expected_names(value, (GO_ARCHIVE,))[0]
    receipt = select_receipt(inp, tag)
    out = Path(env["BRIDGE_OUT_DIR"])
    if out.exists() and any(out.iterdir()):
        raise ValueError("The bridge output folder must be empty")
    out.mkdir(parents=True, exist_ok=True)
    script = source / "scripts" / "package-bridge.ps1"
    if script.is_symlink() or not script.is_file():
        raise ValueError("The reviewed bridge script is missing")
    args = ["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
            "-Version", value, "-Archive", str(inp / archive), "-Receipt", str(inp / receipt), "-OutDir", str(out.resolve())]
    result = run(args, cwd=source, env=_child_env(env), check=False)
    if result.returncode:
        raise RuntimeError(f"package-bridge.ps1 failed with exit {result.returncode}")
    return verify_outputs(out, value)


def draft(tag, out, request=api, create=subprocess.run):
    """Creates the bridge draft. Refused when the destination already has this tag (draft or published) or is not the expected repo."""
    value = bridge_version(tag)
    meta = request("GET", "repos/" + BRIDGE_DESTINATION)
    if (meta.get("full_name") != BRIDGE_DESTINATION or meta.get("private") is not False or meta.get("archived") is not False
            or meta.get("disabled") is not False or (meta.get("owner") or {}).get("login") != "AIOSCompanion"):
        raise ValueError("The bridge destination identity or visibility changed")
    latest_before = request("GET", f"repos/{BRIDGE_DESTINATION}/releases/latest").get("tag_name")
    if releases_for(BRIDGE_DESTINATION, tag, request):
        raise ValueError("The bridge destination already has a release for this version; delete that draft first")
    files = verify_outputs(out, value)
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        raise ValueError("The publishing credential is required to create the draft")
    args = ["gh", "release", "create", f"v{value}", "--draft", "--repo", BRIDGE_DESTINATION, "--title", value,
            "--notes", NOTES, *[str(path) for path in files.values()]]
    result = create(args, env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"gh release create failed with exit {result.returncode}")
    found = single_draft(releases_for(BRIDGE_DESTINATION, tag, request))
    if found is None or found.get("draft") is not True or found.get("prerelease") is not False:
        raise ValueError("The created release is not an unpublished draft")
    assets = {item["name"]: item for item in found.get("assets", [])}
    for name, path in files.items():
        if name not in assets or assets[name].get("size") != path.stat().st_size or assets[name].get("digest") != "sha256:" + digest(path):
            raise ValueError(f"Draft asset {name} does not match the verified output")
    if request("GET", f"repos/{BRIDGE_DESTINATION}/releases/latest").get("tag_name") != latest_before:
        raise ValueError("The latest published release changed during the draft")
    return {"draft_id": found.get("id"), "assets": sorted(assets)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: bridge_release.py fetch|build|draft", file=sys.stderr)
        return 2
    env = dict(os.environ)
    try:
        command = argv[0]
        if command == "fetch":
            names = fetch(env.get("TAG", ""), env.get("APP_RELEASE_REPOSITORY", ""), env["BRIDGE_INPUT_DIR"])
            print("Verified Go release inputs: " + ", ".join(names))
        elif command == "build":
            files = build(env)
            print("Built bridge outputs: " + ", ".join(sorted(p.name for p in files.values())))
        elif command == "draft":
            result = draft(env.get("TAG", ""), env["BRIDGE_OUT_DIR"])
            print(f"Draft created in the bridge destination: {result['assets']}")
        else:
            raise ValueError("unknown command")
        return 0
    except Exception as error:
        import traceback
        traceback.print_exc()
        print(f"Bridge release gate failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
