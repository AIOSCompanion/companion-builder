"""Sign, notarize, staple and package the new app after its credential-free build."""
import base64
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import subprocess
import tempfile

from app_release_artifacts import version


def run(args, timeout=180):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except Exception:
        raise RuntimeError("macOS release tool failed; arguments and output withheld") from None
    if result.returncode:
        raise RuntimeError("macOS release tool rejected the operation; arguments and output withheld")
    return result.stdout


def package(root, tag, env, unsigned=False):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("The macOS bundle must be validated on a native arm64 runner")
    root = Path(root)
    bundle = root / f"Companion_{version(tag)}_darwin_arm64.app"
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("Expected macOS bundle is missing")
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    executable = info.get("CFBundleExecutable", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", executable) or not (bundle / "Contents/MacOS" / executable).is_file():
        raise ValueError("macOS bundle executable is invalid")
    if str(info.get("CFBundleShortVersionString")) != version(tag).split("-", 1)[0]:
        raise ValueError("macOS bundle version does not match the reviewed tag")
    if run([str(bundle / "Contents/MacOS" / executable), "version"]).decode().strip() != version(tag):
        raise ValueError("macOS executable does not report the reviewed version")
    suffix = "_unsigned-test" if unsigned else ""
    archive = root / f"Companion_{version(tag)}_darwin_arm64{suffix}.zip"
    if archive.exists():
        raise ValueError("Refusing to reuse a previous macOS archive")
    if unsigned:
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(archive)])
        return
    required = ("CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER")
    if any(not env.get(name) for name in required):
        raise ValueError("Maintained macOS signing credentials are incomplete")
    with tempfile.TemporaryDirectory(prefix="app-signing-") as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        certificate = folder / "certificate.p12"
        certificate.write_bytes(base64.b64decode(env["CSC_LINK"], validate=True))
        certificate.chmod(0o600)
        private_key = folder / "AuthKey.p8"
        private_key.write_text(env["APPLE_API_KEY"], encoding="utf-8")
        private_key.chmod(0o600)
        keychain = folder / "release.keychain-db"
        password = secrets.token_hex(32)
        created = False
        try:
            run(["security", "create-keychain", "-p", password, str(keychain)])
            created = True
            run(["security", "unlock-keychain", "-p", password, str(keychain)])
            run(["security", "import", str(certificate), "-k", str(keychain), "-P", env["CSC_KEY_PASSWORD"],
                 "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"])
            run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:", "-s", "-k", password, str(keychain)])
            identities = run(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)]).decode()
            found = re.findall(r'\b([A-F0-9]{40})\s+"Developer ID Application:[^"\n]+"', identities)
            if len(found) != 1:
                raise ValueError("Exactly one valid Developer ID Application identity is required")
            identity = found[0]
            sign = ["codesign", "--force", "--options", "runtime", "--timestamp", "--keychain", str(keychain), "--sign", identity]
            # Sign nested Mach-O objects before their containing bundles.
            magic = {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
            entries = sorted(bundle.rglob("*"), key=lambda path: len(path.parts), reverse=True)
            for path in entries:
                if path.is_symlink():
                    continue
                if path.is_file():
                    with path.open("rb") as source:
                        macho = source.read(4) in magic
                    if macho:
                        run(sign + [str(path)])
                elif path.suffix in (".framework", ".xpc", ".appex", ".app"):
                    run(sign + [str(path)])
            run(sign + [str(bundle)])
            run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(bundle)])
            submission = folder / "submission.zip"
            run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(submission)])
            result = json.loads(run(["xcrun", "notarytool", "submit", str(submission), "--key", str(private_key),
                "--key-id", env["APPLE_API_KEY_ID"], "--issuer", env["APPLE_API_ISSUER"], "--wait", "--output-format", "json"], timeout=1800))
            if result.get("status") != "Accepted":
                raise ValueError("Apple did not accept the notarization submission")
            run(["xcrun", "stapler", "staple", str(bundle)])
            run(["xcrun", "stapler", "validate", str(bundle)])
            run(["spctl", "--assess", "--type", "execute", "--verbose", str(bundle)])
            run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(archive)])
        finally:
            if created:
                run(["security", "delete-keychain", str(keychain)])
