"""Strict allowlists and content receipts for the new app's downloadable assets."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import zipfile

TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
SHA = re.compile(r"[a-f0-9]{40}")


def version(tag):
    if not TAG.fullmatch(tag or ""):
        raise ValueError("Invalid new-app version tag")
    return tag[1:]


WINDOWS_RESOURCE_FILES = {
    "resources/external_runtimes/node-win-x64/node.exe",
    "resources/external_runtimes/node-win-x64/LICENSE",
    "resources/external_runtimes/python-win-x64/python.exe",
    "resources/agent_browser_guard/agent-browser-guard.mjs",
    "resources/arc_desktop/arc-desktop.exe",
    "resources/android_mcp_deps/python-deps/adbutils/binaries/adb.exe",
    "resources/default-mcps/companion-google/server.mjs",
    "resources/default-mcps/companion-m365/server.mjs",
    *(f"resources/default-mcps/{name}/mcp.json" for name in
      ("companion-google", "companion-m365", "android-commander", "agent-browser-guard", "arc-desktop")),
}
WINDOWS_RESOURCE_TREES = {
    "resources/external_runtimes/python-win-x64/",
    "resources/android_mcp_deps/",
    "resources/agent_browser_guard/",
    "resources/arc_desktop/",
    *(f"resources/default-mcps/{name}/" for name in
      ("companion-google", "companion-m365", "android-commander", "agent-browser-guard", "arc-desktop")),
}

# Receipt generations. A Windows release publishes one receipt per generation, and a generation names an updater's
# allow-list: generation 1 the list the 0.19.1 to 0.19.3 updaters read, generation 2 the list the new updater reads
# first. A generation-G receipt names the newest published release whose archive extracts under GENERATIONS[G]: this
# release when its archive does, otherwise the previous published release's G receipt, copied byte for byte. Why a copy
# is needed: companion-engine docs/release-order.md.

# Generation 1 is copied from `git show v0.19.1:internal/desktop/update_archive.go` in companion-engine (tag v0.19.1,
# commit d71429694018a80240f179c0a25ab551e9088687): its releaseResourceFiles and releaseResourceTrees. They are kept
# literal, so widening WINDOWS_RESOURCE_FILES or WINDOWS_RESOURCE_TREES never widens what the 0.19.1 updater accepts.
GEN1_RESOURCE_FILES = frozenset({
    "resources/external_runtimes/node-win-x64/node.exe",
    "resources/external_runtimes/node-win-x64/LICENSE",
    "resources/external_runtimes/python-win-x64/python.exe",
    "resources/agent_browser_guard/agent-browser-guard.mjs",
    "resources/arc_desktop/arc-desktop.exe",
    "resources/android_mcp_deps/python-deps/adbutils/binaries/adb.exe",
    "resources/default-mcps/companion-google/server.mjs",
    "resources/default-mcps/companion-m365/server.mjs",
    "resources/default-mcps/companion-google/mcp.json",
    "resources/default-mcps/companion-m365/mcp.json",
    "resources/default-mcps/android-commander/mcp.json",
    "resources/default-mcps/agent-browser-guard/mcp.json",
    "resources/default-mcps/arc-desktop/mcp.json",
})
GEN1_RESOURCE_TREES = frozenset({
    "resources/external_runtimes/python-win-x64/",
    "resources/android_mcp_deps/",
    "resources/agent_browser_guard/",
    "resources/arc_desktop/",
    "resources/default-mcps/companion-google/",
    "resources/default-mcps/companion-m365/",
    "resources/default-mcps/android-commander/",
    "resources/default-mcps/agent-browser-guard/",
    "resources/default-mcps/arc-desktop/",
})
# Generation 2 admits, and does not require, the two Companion Browser (ARC) files: AdmittedResourceFiles in
# internal/releasearchive/archive.go (companion-engine commit 4182aa3e147d23e3a1b5b369cfb41fcc59139bf3). Its required
# files and trees are generation 1's.
GEN2_ADMITTED_FILES = frozenset({
    "resources/companion_arc/companion-arc.cjs",
    "resources/default-mcps/companion-arc/mcp.json",
})
GENERATIONS = {
    1: {"required": GEN1_RESOURCE_FILES, "files": GEN1_RESOURCE_FILES, "trees": GEN1_RESOURCE_TREES},
    2: {"required": GEN1_RESOURCE_FILES, "files": GEN1_RESOURCE_FILES | GEN2_ADMITTED_FILES,
        "trees": GEN1_RESOURCE_TREES},
}
# The receipt each platform publishes per generation. Generation 1 keeps the name the older updaters read.
RECEIPTS = {
    "windows-amd64": {1: "companion-release-windows-amd64.json", 2: "companion-release-g2-windows-amd64.json"},
    "darwin-arm64": {1: "companion-release-darwin-arm64.json"},
}


def windows_binaries(tag):
    value = version(tag)
    return [f"Companion_{value}_windows_amd64.exe", f"companion-cli_{value}_windows_amd64.exe"]


def filenames(tag, platform, unsigned=False):
    value = version(tag)
    if platform == "windows-amd64":
        return [f"Companion_{value}_windows_amd64.zip"]
    if platform == "darwin-arm64":
        suffix = "_unsigned-test" if unsigned else ""
        return [f"Companion_{value}_darwin_arm64{suffix}.zip"]
    raise ValueError("Unsupported new-app platform")


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def regular_file(root, name):
    file = Path(root) / name
    if file.is_symlink() or not file.is_file() or file.resolve().parent != Path(root).resolve() or not file.stat().st_size:
        raise ValueError("Expected release asset is missing, empty or not a regular local file")
    return file


def verify_windows_outputs(root, tag):
    names = windows_binaries(tag)
    values = {name: digest(regular_file(root, name)) for name in names}
    manifest = regular_file(root, "SHA256SUMS").read_text(encoding="utf-8-sig")
    listed = {}
    for line in manifest.splitlines():
        match = re.fullmatch(r"([a-fA-F0-9]{64}) [ *]([^/\\]+)", line)
        if not match or match[2] in listed:
            raise ValueError("Build checksum manifest is malformed or ambiguous")
        listed[match[2]] = match[1].lower()
    if listed != values:
        raise ValueError("Build checksum manifest does not bind exactly the two Windows binaries")
    return names


def package_windows(root, tag):
    root = Path(root)
    binaries = verify_windows_outputs(root, tag)
    resources = root / "resources"
    if not resources.is_dir() or resources.is_symlink() or resources.is_junction():
        raise ValueError("Windows native resources are missing")
    members = dict((name, root / name) for name in binaries)
    for path in sorted(resources.rglob("*")):
        info = path.lstat()
        if path.is_symlink() or path.is_junction() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("Windows release resources contain a link or special file")
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if name not in WINDOWS_RESOURCE_FILES and not any(name.startswith(prefix) for prefix in WINDOWS_RESOURCE_TREES):
            raise ValueError("Windows archive contains an unexpected resource")
        if any(part.startswith((".env", ".git")) and part not in (".github", ".gitignore") for part in path.relative_to(root).parts):
            raise ValueError("Windows release resources contain a local configuration or repository")
        members[name] = path
    if not WINDOWS_RESOURCE_FILES.issubset(members) or any(members[name].stat().st_size == 0 for name in WINDOWS_RESOURCE_FILES):
        raise ValueError("Windows release is missing a required Companion MCP resource")
    if len({name.casefold() for name in members}) != len(members):
        raise ValueError("Windows release paths collide")
    return write_release_archive(root / filenames(tag, "windows-amd64")[0], members)


def write_release_archive(archive, members):
    """Write the members ({archive name: file}) to a new archive, the one zip writer the Windows packer uses."""
    archive = Path(archive)
    try:
        with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=5) as output:
            for name, path in members.items():
                output.write(path, name)
    except FileExistsError:
        raise ValueError("The Windows release archive already exists") from None
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    return archive


def signing_for(platform, unsigned=False):
    if platform == "darwin-arm64":
        return "unsigned-test" if unsigned else "signed-notarized"
    return "unsigned-windows"


def receipt_names(platform):
    if platform not in RECEIPTS:
        raise ValueError("Unsupported new-app platform")
    return [RECEIPTS[platform][generation] for generation in sorted(RECEIPTS[platform])]


def archive_names(archive):
    try:
        with zipfile.ZipFile(archive) as bundle:
            return bundle.namelist()
    except zipfile.BadZipFile:
        raise ValueError("The Windows release archive is not a valid zip") from None


def generation_problems(names, tag, generation):
    """Why an archive with these entries does not extract under GENERATIONS[generation]; empty when it does.

    The program and the console binary are always admitted. Any other entry must be listed or under a tree, and every
    required file must be present, as the updater checks them.
    """
    spec = GENERATIONS[generation]
    program, cli = windows_binaries(tag)
    problems = [f"unexpected entry {name}" for name in names if not name.endswith("/") and name not in (program, cli)
                and name not in spec["files"] and not any(name.startswith(tree) for tree in spec["trees"])]
    problems += [f"missing {name}" for name in sorted(({program} | spec["required"]) - set(names))]
    return problems


def validate_pointer(raw, platform, tag):
    """A copied receipt of an older published release, accepted only when it is self-consistent.

    It must name another version, and its schema, platform, signing mode and asset names must describe that version.
    Its archive is not here to hash, so the release it names is checked against GitHub's listing instead.
    """
    try:
        receipt = json.loads(raw)
    except ValueError:
        raise ValueError("Copied pointer receipt is not JSON") from None
    older = receipt.get("tag") if isinstance(receipt, dict) else None
    if not isinstance(older, str) or older == tag:
        raise ValueError("Copied pointer receipt does not name an older release")
    value = version(older)
    assets = receipt.get("assets")
    if (receipt.get("schema") != 1 or receipt.get("platform") != platform or receipt.get("version") != value
            or receipt.get("signing") != signing_for(platform) or not isinstance(receipt.get("source_sha"), str)
            or not SHA.fullmatch(receipt["source_sha"]) or not isinstance(assets, dict)
            or set(assets) != set(filenames(older, platform)) | {f"SHA256SUMS-{platform}.txt"}
            or not all(isinstance(hash, str) and re.fullmatch(r"[a-f0-9]{64}", hash) for hash in assets.values())):
        raise ValueError("Copied pointer receipt is not self-consistent")
    return receipt


def windows_receipts(archive, tag, own, previous=None):
    """The Windows receipts of this release, by file name, as the exact bytes to publish.

    A generation the archive extracts under gets `own`. Any other gets the newest published release's receipt of that
    generation, copied byte for byte: previous(name) returns it, or None when that release has none. A missing or
    invalid copy refuses the release, so a pointer is never fabricated.
    """
    entries = archive_names(archive)
    receipts = {}
    for generation, name in sorted(RECEIPTS["windows-amd64"].items()):
        problems = generation_problems(entries, tag, generation)
        if not problems:
            receipts[name] = own
            continue
        copy = previous(name) if previous else None
        if copy is None:
            raise ValueError(f"This release's archive does not extract under generation {generation} "
                             f"({'; '.join(problems[:3])}), and the newest published release has no valid {name} "
                             "to copy; refusing to publish without a pointer")
        validate_pointer(copy, "windows-amd64", tag)
        receipts[name] = copy
    return receipts


def prepare(root, output, tag, source_sha, platform, unsigned=False, previous=None):
    if not SHA.fullmatch(source_sha or ""):
        raise ValueError("Invalid immutable source commit")
    root, output = Path(root), Path(output)
    if output.exists():
        raise ValueError("Release staging directory already exists")
    output.mkdir(parents=True, mode=0o700)
    if platform == "windows-amd64":
        package_windows(root, tag)
    names = filenames(tag, platform, unsigned)
    hashes = {}
    for name in names:
        source = regular_file(root, name)
        hashes[name] = digest(source)
        shutil.copyfile(source, output / name)
        if digest(output / name) != hashes[name]:
            raise ValueError("Asset changed while staging")
    checksum = f"SHA256SUMS-{platform}.txt"
    (output / checksum).write_text("".join(f"{hashes[name]}  {name}\n" for name in names), encoding="utf-8", newline="\n")
    hashes[checksum] = digest(output / checksum)
    receipt = {"schema": 1, "source_sha": source_sha, "tag": tag, "version": version(tag),
               "platform": platform, "signing": signing_for(platform, unsigned), "assets": hashes}
    own = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if platform == "windows-amd64":
        copies = windows_receipts(output / names[0], tag, own, previous)
    else:
        copies = {receipt_names(platform)[0]: own}
    for name, content in copies.items():
        (output / name).write_bytes(content)
    return receipt


def inspect(output, tag, source_sha, platform, unsigned=False):
    """Check a staged folder and return (the generation-1 receipt, the files to upload).

    Each receipt must match the archive it describes: a generation the archive extracts under holds this release's own
    receipt, any other holds a valid copy of an older release's receipt.
    """
    output = Path(output)
    archive = filenames(tag, platform, unsigned)[0]
    expected_names = {archive, f"SHA256SUMS-{platform}.txt"}
    raws = {generation: regular_file(output, name).read_bytes() for generation, name in RECEIPTS[platform].items()}
    # Every receipt that claims this release is checked against the files it hashes before the archive is read.
    claimed = {}
    for generation, raw in raws.items():
        receipt = json.loads(raw)
        if isinstance(receipt, dict) and receipt.get("tag") == tag:
            if (receipt.get("schema") != 1 or receipt.get("source_sha") != source_sha
                    or receipt.get("version") != version(tag) or receipt.get("platform") != platform
                    or receipt.get("signing") != signing_for(platform, unsigned)
                    or set(receipt.get("assets", {})) != expected_names):
                raise ValueError("Release receipt does not match the reviewed build")
            for file, value in receipt["assets"].items():
                if not re.fullmatch(r"[a-f0-9]{64}", value or "") or digest(regular_file(output, file)) != value:
                    raise ValueError("Release asset hash changed")
            claimed[generation] = receipt
    if platform == "windows-amd64":
        entries = archive_names(regular_file(output, archive))
        own = {generation: not generation_problems(entries, tag, generation) for generation in GENERATIONS}
    else:
        own = {1: True}
    receipts = {}
    for generation in RECEIPTS[platform]:
        if own[generation]:
            if generation not in claimed:
                raise ValueError("Release receipt does not match the reviewed build")
            receipts[generation] = claimed[generation]
        else:
            receipts[generation] = validate_pointer(raws[generation], platform, tag)
    expected = expected_names | set(receipt_names(platform))
    if {p.name for p in output.iterdir()} != expected:
        raise ValueError("Unexpected files in release staging")
    return receipts[1], [output / name for name in sorted(expected)]
