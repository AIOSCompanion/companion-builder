"""Sign, notarize, staple and package the new app after its credential-free build.

Runs inside run_sealed.py. The tools' own output goes into the sealed log only, so a rejected certificate,
signature or notarisation can be diagnosed from the decrypted log. No secret reaches a tool's environment, an
error message or this output; the certificate password reaches `security import` as an argument, as it does in
electron-builder, which the README's dependency residual covers.

Signing works on a copy of the engine's unsigned bundle named Companion.app, so the download unpacks to the
app's own name:
  1. the certificate and the notary key go into a private temporary folder (0700, files 0600);
  2. a temporary keychain (random password, no auto-lock) receives Apple's Developer ID intermediates, pinned by
     SHA-256, and the certificate; it joins the user search list so codesign can build the signer's chain;
  3. the one valid Developer ID Application identity signs nested code deepest first, then the bundle, with the
     hardened runtime and a secure timestamp;
  4. Apple's notary service must answer Accepted (its log is fetched into the sealed output otherwise);
  5. the ticket is stapled (retried while Apple propagates it) and validated, and Gatekeeper must assess the app
     as notarised Developer ID software;
  6. the stapled app is zipped. verify_signed_archive checks that ZIP again, independently, before staging.
On every exit the search list is restored, the keychain deleted and the folder removed.
"""
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import secrets
import subprocess
import tempfile
import time
import uuid

from app_release_artifacts import version

APP_NAME = "Companion.app"
# All five signing values. notarytool receives the key ID and issuer as arguments; no tool needs any of them in its
# environment.
SECRET_NAMES = frozenset(("CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER"))
# Apple's Developer ID intermediates (https://www.apple.com/certificateauthority/), both issued by Apple Root CA
# (SHA-256 B0B1730ECBC7FF4505142C49F1295E6EDA6BCAED7E2C68C5BE91B5A11001F024). codesign needs the one that issued
# the team's certificate to build the chain; electron-builder ships its own copies for the same reason.
INTERMEDIATES = {
    "DeveloperIDCA.cer": "7afc9d01a62f03a2de9637936d4afe68090d2de18d03f29c88cfb0b1ba63587f",
    "DeveloperIDG2CA.cer": "f16cd3c54c7f83cea4bf1a3e6a0819c8aaa8e4a1528fd144715f350643d2df3a",
}
CERTIFICATES = Path(__file__).resolve().parent / "apple"
MACHO_MAGICS = frozenset({b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
                          b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"})
NESTED_BUNDLES = (".framework", ".xpc", ".appex", ".app")
UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
STAPLE_ATTEMPTS, STAPLE_PAUSE = 5, 20


def scrubbed(env=None):
    """A child environment without the raw certificate, its password or the notary key. No tool needs them there."""
    return {name: value for name, value in (os.environ if env is None else env).items() if name not in SECRET_NAMES}


def attempt(args, timeout=180, env=None):
    """Run a tool and print its output into the sealed log. Returns (exit code, combined output, standard output)."""
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False, env=scrubbed(env))
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(args[0]).name} {args[1] if len(args) > 1 else ''} timed out after {timeout} s") from None
    stdout = (result.stdout or b"").decode("utf-8", "replace")
    output = stdout + (result.stderr or b"").decode("utf-8", "replace")
    if output.strip():
        print(output.rstrip())
    return result.returncode, output, stdout


def run(args, timeout=180, env=None):
    """Run a tool that must succeed. Returns its standard output. The error names the tool, never its arguments."""
    code, _, stdout = attempt(args, timeout, env)
    if code:
        raise RuntimeError(f"{Path(args[0]).name} {args[1] if len(args) > 1 else ''} failed with exit {code}")
    return stdout


def bundle_executable(bundle, tag):
    """The bundle's structure and Info.plist, without executing anything in it. Returns its executable."""
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    executable = info.get("CFBundleExecutable", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", executable) or not (bundle / "Contents/MacOS" / executable).is_file():
        raise ValueError("macOS bundle executable is invalid")
    if str(info.get("CFBundleShortVersionString")) != version(tag).split("-", 1)[0]:
        raise ValueError("macOS bundle version does not match the reviewed tag")
    return bundle / "Contents/MacOS" / executable


def check_bundle(bundle, tag):
    """The engine's bundle: its Info.plist names an executable that reports this release's version.

    This executes the app, so it runs only in steps that hold no signing material: the credential-free check
    before signing, the unsigned test packaging, and the staging check of the signed ZIP."""
    if run([str(bundle_executable(bundle, tag)), "version"]).strip() != version(tag):
        raise ValueError("macOS executable does not report the reviewed version")


def zip_app(app, archive, env=None):
    run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app), str(archive)], timeout=900, env=env)


def signing_inputs(env):
    try:
        certificate = base64.b64decode("".join(env.get("CSC_LINK", "").split()), validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("CSC_LINK is not base64 text of the Developer ID .p12 certificate") from None
    password = env.get("CSC_KEY_PASSWORD", "")
    key, key_id, issuer = env.get("APPLE_API_KEY", ""), env.get("APPLE_API_KEY_ID", ""), env.get("APPLE_API_ISSUER", "")
    if not certificate or not password or not key or not key_id or not issuer:
        raise ValueError("Maintained macOS signing credentials are incomplete")
    if not key.lstrip().startswith("-----BEGIN PRIVATE KEY-----") or not re.fullmatch(r"[A-Z0-9]{10}", key_id):
        raise ValueError("APPLE_API_KEY or APPLE_API_KEY_ID is malformed")
    try:
        uuid.UUID(issuer)
    except ValueError:
        raise ValueError("APPLE_API_ISSUER is not an App Store Connect issuer ID") from None
    return certificate, password, key, key_id, issuer


def write_private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)


def user_keychains(env):
    return [line.strip().strip('"') for line in run(["security", "list-keychains", "-d", "user"], env=env).splitlines()
            if line.strip()]


def find_identity(keychain, env):
    identities = run(["security", "find-identity", "-v", "-p", "codesigning", str(keychain)], env=env)
    found = re.findall(r'\b([A-F0-9]{40})\s+"Developer ID Application:[^"\n]+"', identities)
    if len(found) != 1:
        raise ValueError(f"Exactly one valid Developer ID Application identity is required; found {len(found)}")
    return found[0]


def nested_code(app):
    """Nested code to sign before the bundle, deepest first: Mach-O files and nested bundles."""
    entries = sorted(app.rglob("*"), key=lambda path: len(path.parts), reverse=True)
    nested = []
    for path in entries:
        if path.is_symlink():
            continue
        if path.is_file():
            with path.open("rb") as source:
                if source.read(4) in MACHO_MAGICS:
                    nested.append(path)
        elif path.suffix in NESTED_BUNDLES:
            nested.append(path)
    return nested


def notarize(submission, key, key_id, issuer, env):
    credentials = ["--key", str(key), "--key-id", key_id, "--issuer", issuer]
    code, _, stdout = attempt(["xcrun", "notarytool", "submit", str(submission), *credentials, "--wait", "--timeout", "45m",
                               "--output-format", "json"], timeout=3000, env=env)
    verdict = {}
    for candidate in [stdout, *reversed(stdout.splitlines())]:
        try:
            verdict = json.loads(candidate)
            break
        except ValueError:
            continue
    if not isinstance(verdict, dict):
        verdict = {}
    if code == 0 and verdict.get("status") == "Accepted":
        return
    submission_id = verdict.get("id")
    if isinstance(submission_id, str) and UUID_TEXT.fullmatch(submission_id):
        attempt(["xcrun", "notarytool", "log", submission_id, *credentials], timeout=300, env=env)
    raise ValueError(f"Apple did not accept the notarization submission (status: {verdict.get('status') or 'none'})")


def staple(app, env, sleep=time.sleep):
    for number in range(1, STAPLE_ATTEMPTS + 1):
        if attempt(["xcrun", "stapler", "staple", str(app)], timeout=300, env=env)[0] == 0:
            return
        if number < STAPLE_ATTEMPTS:
            sleep(STAPLE_PAUSE)
    raise RuntimeError("The notarization ticket could not be stapled")


def assess(app, env=None):
    """Gatekeeper must accept the app as notarised Developer ID software."""
    code, output, _ = attempt(["spctl", "--assess", "--type", "execute", "--verbose=4", str(app)], env=env)
    if code == 0 and "source=Notarized Developer ID" in output:
        return
    if "override=security disabled" in output:
        # Assessments are switched off on this runner, so spctl proves nothing; the stapled ticket was validated.
        print("Gatekeeper assessments are disabled on this runner; relying on the validated stapled ticket")
        return
    raise ValueError("Gatekeeper does not accept the app as notarised Developer ID software")


def verify_signature(app, env=None):
    """Developer ID Application, a team, a secure timestamp and the hardened runtime; then the stapled ticket."""
    run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], env=env)
    code, details, _ = attempt(["codesign", "-d", "--verbose=4", str(app)], env=env)
    authorities = re.findall(r"^Authority=(.+)$", details, flags=re.M)
    if (code or not authorities or not authorities[0].startswith("Developer ID Application: ")
            or authorities[-1] != "Apple Root CA"):
        raise ValueError("The app is not signed with a Developer ID Application certificate")
    if not re.search(r"^TeamIdentifier=[A-Z0-9]{10}$", details, flags=re.M):
        raise ValueError("The app's signature names no team")
    if not re.search(r"^Timestamp=", details, flags=re.M):
        raise ValueError("The app's signature has no secure timestamp")
    if not re.search(r"flags=0x[0-9a-f]+\([^)]*\bruntime\b[^)]*\)", details):
        raise ValueError("The app is not signed with the hardened runtime")
    run(["xcrun", "stapler", "validate", str(app)], env=env)
    assess(app, env)


def sign_notarize_staple(app, folder, env, sleep=time.sleep):
    certificate, password, key, key_id, issuer = signing_inputs(env)
    for name, expected in INTERMEDIATES.items():
        if hashlib.sha256((CERTIFICATES / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Apple intermediate {name} does not match its pinned hash")
    child = {name: value for name, value in env.items() if name not in SECRET_NAMES}
    certificate_path, key_path = folder / "certificate.p12", folder / f"AuthKey_{key_id}.p8"
    write_private(certificate_path, certificate)
    write_private(key_path, key.encode("utf-8"))
    keychain, keychain_password = folder / "release.keychain-db", secrets.token_hex(32)
    original, created, failed = user_keychains(child), False, True
    try:
        run(["security", "create-keychain", "-p", keychain_password, str(keychain)], env=child)
        created = True
        run(["security", "set-keychain-settings", "-lut", "21600", str(keychain)], env=child)
        run(["security", "unlock-keychain", "-p", keychain_password, str(keychain)], env=child)
        for name in INTERMEDIATES:
            run(["security", "import", str(CERTIFICATES / name), "-k", str(keychain)], env=child)
        run(["security", "import", str(certificate_path), "-k", str(keychain), "-f", "pkcs12", "-P", password,
             "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"], env=child)
        run(["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", keychain_password,
             str(keychain)], env=child)
        run(["security", "list-keychains", "-d", "user", "-s", str(keychain), *original], env=child)
        identity = find_identity(keychain, child)
        sign = ["codesign", "--force", "--options", "runtime", "--timestamp", "--keychain", str(keychain), "--sign", identity]
        for path in [*nested_code(app), app]:
            run(sign + [str(path)], timeout=600, env=child)
        run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], env=child)
        submission = folder / "submission.zip"
        zip_app(app, submission, env=child)
        notarize(submission, key_path, key_id, issuer, child)
        staple(app, child, sleep)
        verify_signature(app, child)
        failed = False
    finally:
        problems = []
        if created:
            if attempt(["security", "list-keychains", "-d", "user", "-s", *original], env=child)[0]:
                problems.append("the keychain search list could not be restored")
            if attempt(["security", "delete-keychain", str(keychain)], env=child)[0]:
                problems.append("the temporary keychain could not be deleted")
        for path in (certificate_path, key_path):
            path.unlink(missing_ok=True)
        if problems and not failed:
            raise RuntimeError("Signing cleanup failed: " + "; ".join(problems))
        for problem in problems:
            print("Signing cleanup: " + problem)


def source_bundle(root, tag):
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ValueError("The macOS bundle must be validated on a native arm64 runner")
    bundle = Path(root) / f"Companion_{version(tag)}_darwin_arm64.app"
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("Expected macOS bundle is missing")
    return bundle


def check(root, tag):
    """The credential-free step before signing: the built bundle reports this release's version."""
    check_bundle(source_bundle(root, tag), tag)
    print("macOS bundle structure and version verified before signing")


def package(root, tag, env, unsigned=False, sleep=time.sleep):
    root = Path(root)
    bundle = source_bundle(root, tag)
    if unsigned:
        check_bundle(bundle, tag)  # this step holds no signing material
    else:
        # The signing step runs Apple's tools only: the app was executed by the credential-free check before
        # this step, and its signed copy is executed again by the staging check, which holds no secrets.
        bundle_executable(bundle, tag)
    suffix = "_unsigned-test" if unsigned else ""
    archive = root / f"Companion_{version(tag)}_darwin_arm64{suffix}.zip"
    if archive.exists():
        raise ValueError("Refusing to reuse a previous macOS archive")
    with tempfile.TemporaryDirectory(prefix="app-signing-", dir=env.get("RUNNER_TEMP") or None) as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        app = folder / "bundle" / APP_NAME
        run(["ditto", str(bundle), str(app)], timeout=600)
        if not unsigned:
            sign_notarize_staple(app, folder, env, sleep)
        zip_app(app, archive)
    print(f"{archive.name} created: Companion.app " + ("UNSIGNED (explicit test build)" if unsigned else
                                                         "signed, notarised and stapled"))


def verify_signed_archive(root, tag):
    """The signed ZIP, checked independently of the step that made it: exactly Companion.app, Developer ID signed,
    notarised and stapled, reporting this release's version."""
    if platform.system() != "Darwin":
        raise ValueError("The signed macOS archive must be verified on macOS")
    archive = Path(root) / f"Companion_{version(tag)}_darwin_arm64.zip"
    if archive.is_symlink() or not archive.is_file() or not archive.stat().st_size:
        raise ValueError("The signed macOS archive is missing")
    with tempfile.TemporaryDirectory(prefix="app-verify-", dir=os.environ.get("RUNNER_TEMP") or None) as temporary:
        run(["ditto", "-x", "-k", str(archive), temporary], timeout=900)
        if sorted(path.name for path in Path(temporary).iterdir()) != [APP_NAME]:
            raise ValueError("The macOS archive must hold exactly Companion.app")
        app = Path(temporary) / APP_NAME
        check_bundle(app, tag)
        verify_signature(app)
    print(f"{archive.name} verified: Developer ID signed, notarised and stapled")
