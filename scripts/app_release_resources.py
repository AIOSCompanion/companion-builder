"""Build the legacy Companion MCP resources on the same native runner as Go."""
import hashlib
from pathlib import Path, PurePosixPath
import plistlib
import shutil
import sys
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_release_artifacts import digest, version
from app_release_checkout import RESOURCE_SHA
from app_release import checked


RUNTIME_URL = "https://github.com/AIOSCompanion/aios-companion-releases/releases/download/runtimes-v1/"
WINDOWS_NODE = "node-v22.23.3-win-x64"
WINDOWS_NODE_SHA = "2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71"
WINDOWS_NODE_EXE_SHA = "9c9245166b4a8e182e0b797da9c20136117ff24368eaff1fec8343a123c8db0e"
PLATFORM_INPUTS = {
    "windows-amd64": (
        "external_runtimes_win.tar.gz",
        "d18a6b2af4360211f73ac8ddc1f5669193210fed991d4a774d64b871ca9f86db",
        84661583,
        ["python-win-x64"],
    ),
    "darwin-arm64": (
        "external_runtimes_mac.tar.gz",
        "2159c01e65006583a98610ef89e87ba6df1ffffc144d711d1c036b4cb0d7428d",
        74581060,
        ["python-mac-arm64", "node-mac-arm64"],
    ),
}


def download_asset(archive, url, expected_digest, expected_bytes):
    contents = hashlib.sha256()
    remaining = expected_bytes
    with urlopen(url, timeout=60) as response, archive.open("xb") as output:
        while block := response.read(min(1 << 20, remaining + 1)):
            remaining -= len(block)
            if remaining < 0:
                raise ValueError("Runtime archive exceeded its reviewed size")
            contents.update(block)
            output.write(block)
    if remaining or contents.hexdigest() != expected_digest:
        raise ValueError("Runtime archive differs from the reviewed input")


def stage_windows_node(runtimes, scratch):
    archive = scratch / (WINDOWS_NODE + ".zip")
    download_asset(archive, "https://nodejs.org/dist/v22.23.3/" + archive.name, WINDOWS_NODE_SHA, 35574076)
    destination = runtimes / "node-win-x64"
    destination.mkdir()
    with zipfile.ZipFile(archive) as package:
        for filename in ("node.exe", "LICENSE"):
            with package.open(WINDOWS_NODE + "/" + filename) as source, (destination / filename).open("xb") as output:
                shutil.copyfileobj(source, output)
    if digest(destination / "node.exe") != WINDOWS_NODE_EXE_SHA:
        raise ValueError("Windows Node binary does not match the official release")


def runtime_filter(allowed):
    def admit(member, destination):
        parts = PurePosixPath(member.name).parts
        if len(parts) < 2 or parts[0] != "external_runtimes" or parts[1] not in allowed:
            return None
        if any(part.startswith("._") for part in parts):
            return None
        if member.islnk() or (member.issym() and len(PurePosixPath(member.linkname).parts) != 1):
            raise ValueError("Runtime archive contains a link outside its own directory")
        return tarfile.data_filter(member, destination)
    return admit


def extract_runtimes(destination, platform):
    name, archive_digest, length, allowed = PLATFORM_INPUTS[platform]
    with tempfile.TemporaryDirectory(prefix="companion-runtime-") as temporary:
        archive = Path(temporary) / name
        download_asset(archive, RUNTIME_URL + name, archive_digest, length)
        with tarfile.open(archive, "r:gz") as package:
            package.extractall(destination, filter=runtime_filter(allowed))
        if platform == "windows-amd64":
            stage_windows_node(destination / "external_runtimes", Path(temporary))


def stage_resources(source, env, tag, platform):
    legacy = Path(env["GITHUB_WORKSPACE"]).resolve(strict=True) / "resource-source"
    if not legacy.is_dir() or legacy.is_symlink():
        raise ValueError("Pinned resource source is missing")
    if checked(["git", "rev-parse", "HEAD"], cwd=legacy, capture=True) != RESOURCE_SHA:
        raise ValueError("Resource checkout does not match its immutable input")
    if checked(["git", "status", "--porcelain"], cwd=legacy, capture=True):
        raise ValueError("Resource checkout must start with a clean tree")
    child = {key: value for key, value in env.items() if key not in (
        "GH_TOKEN", "GITHUB_TOKEN", "CSC_LINK", "CSC_KEY_PASSWORD",
        "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER",
    )}
    child.pop("NoDefaultCurrentDirectoryInExePath", None)
    runtimes = legacy / "external_runtimes"
    windows = platform == "windows-amd64"
    extract_runtimes(legacy, platform)
    npm = "npm.cmd" if windows else "npm"
    checked([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=legacy / "connectors", env=child)
    checked(["node", "connectors/build.mjs", "--stage"], cwd=legacy, env=child)
    checked(["node", "scripts/stage-agent-browser-guard.mjs"], cwd=legacy, env=child)
    if windows:
        checked(["node", "scripts/stage-arc-desktop.mjs", "--test"], cwd=legacy, env=child)
    row = dict(child, ROW_OS="Windows" if windows else "macOS", ROW_ARCH="x64" if windows else "arm64")
    checked(["bash", "scripts/release/row_android.sh"], cwd=legacy, env=row)

    dist = source / "dist"
    if windows:
        destination = dist / "resources"
    else:
        destination = dist / f"Companion_{version(tag)}_darwin_arm64.app/Contents/Resources"
        if not destination.is_dir() or not (destination / "AppIcon.icns").is_file():
            raise ValueError("App bundle is missing before resource staging")
    checked(["node", str(source / "scripts/stage-desktop-mcps.mjs"), str(legacy), str(runtimes), str(destination)],
            cwd=source, env=child)
    if windows:
        checked([str(destination / "external_runtimes/node-win-x64/node.exe"), "--version"], cwd=destination, env=child)
    else:
        check_macos_runtimes(destination, child)
    print("Staged and verified native Companion MCP resources")


def check_macos_runtimes(resources, env=None):
    node = resources / "external_runtimes/node-mac-arm64/bin/node"
    python = resources / "external_runtimes/python-mac-arm64/bin/python"
    adb = resources / "android_mcp_deps/python-deps/adbutils/binaries/adb"
    # Run real V8 code, import Python with the packaged dependencies, and
    # independently start the bundled adb without reaching a user's devices.
    checked([str(node), "-e", "if ([1,2,3].map(n => n * n).join() !== '1,4,9') process.exit(1)"],
            env=env, timeout=30)
    checked([str(python), "-I", "-S", "-B", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); import mcp.server.fastmcp, uiautomator2",
             str(resources / "android_mcp_deps/python-deps")], env=env, timeout=30)
    checked([str(adb), "version"], env=env, timeout=30)


def macos_public_smoke():
    """Check the public archive and the actual Node signing recipe on arm64."""
    with tempfile.TemporaryDirectory(prefix="companion-mac-inputs-") as temporary:
        root = Path(temporary)
        extract_runtimes(root, "darwin-arm64")
        node = root / "external_runtimes/node-mac-arm64/bin/node"
        python = root / "external_runtimes/python-mac-arm64/bin/python"
        checked([str(python), "-I", "-S", "-B", "-c",
                 "import platform,sys; assert platform.machine()=='arm64' and sys.version_info[:2]==(3,11)"])
        entitlements = root / "jit.plist"
        entitlements.write_bytes(plistlib.dumps({"com.apple.security.cs.allow-jit": True}))
        checked(["codesign", "--force", "--sign", "-", "--options", "runtime", "--entitlements",
                 str(entitlements), str(node)])
        checked(["codesign", "--verify", "--strict", str(node)])
        checked([str(node), "-e", "if (process.arch !== 'arm64' || [1,2,3].map(n => n*n).join() !== '1,4,9') process.exit(1)"])
    print("Pinned Mac runtime archive, Python alias and hardened Node checked on arm64")


if __name__ == "__main__":
    macos_public_smoke()
