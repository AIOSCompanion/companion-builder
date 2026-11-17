"""Strict allowlists and content receipts for the new app's downloadable assets."""
import hashlib
import json
from pathlib import Path
import re
import shutil

TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")
SHA = re.compile(r"[a-f0-9]{40}")


def version(tag):
    if not TAG.fullmatch(tag or ""):
        raise ValueError("Invalid new-app version tag")
    return tag[1:]


def filenames(tag, platform, unsigned=False):
    value = version(tag)
    if platform == "windows-amd64":
        return [f"Companion_{value}_windows_amd64.exe", f"companion-cli_{value}_windows_amd64.exe"]
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
    names = filenames(tag, "windows-amd64")
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


def prepare(root, output, tag, source_sha, platform, unsigned=False):
    if not SHA.fullmatch(source_sha or ""):
        raise ValueError("Invalid immutable source commit")
    root, output = Path(root), Path(output)
    if output.exists():
        raise ValueError("Release staging directory already exists")
    output.mkdir(parents=True, mode=0o700)
    names = verify_windows_outputs(root, tag) if platform == "windows-amd64" else filenames(tag, platform, unsigned)
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
               "platform": platform, "signing": "unsigned-test" if platform == "darwin-arm64" and unsigned else
                   "signed-notarized" if platform == "darwin-arm64" else "unsigned-windows", "assets": hashes}
    receipt_name = f"companion-release-{platform}.json"
    (output / receipt_name).write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return receipt


def inspect(output, tag, source_sha, platform, unsigned=False):
    output = Path(output)
    receipt_name = f"companion-release-{platform}.json"
    receipt = json.loads(regular_file(output, receipt_name).read_text())
    expected_names = set(filenames(tag, platform, unsigned)) | {f"SHA256SUMS-{platform}.txt"}
    expected_signing = "unsigned-test" if platform == "darwin-arm64" and unsigned else (
        "signed-notarized" if platform == "darwin-arm64" else "unsigned-windows")
    if (receipt.get("schema") != 1 or receipt.get("tag") != tag or receipt.get("source_sha") != source_sha
            or receipt.get("version") != version(tag) or receipt.get("platform") != platform
            or receipt.get("signing") != expected_signing or set(receipt.get("assets", {})) != expected_names):
        raise ValueError("Release receipt does not match the reviewed build")
    if {p.name for p in output.iterdir()} != expected_names | {receipt_name}:
        raise ValueError("Unexpected files in release staging")
    for name, value in receipt["assets"].items():
        if not re.fullmatch(r"[a-f0-9]{64}", value or "") or digest(regular_file(output, name)) != value:
            raise ValueError("Release asset hash changed")
    return receipt, [output / name for name in sorted(expected_names | {receipt_name})]
