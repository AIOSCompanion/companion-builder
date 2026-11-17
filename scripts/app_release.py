"""Trusted entry points for the new app; all private/build output stays sealed."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_release_artifacts import SHA, prepare, version


def checked(args, *, cwd=None, env=None, capture=False, timeout=1800):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=capture, text=True, check=False, timeout=timeout)
    if result.returncode:
        raise RuntimeError("New-app release operation failed; output remains sealed")
    return result.stdout.strip() if capture else None


def source_identity(env):
    tag, sha, object_sha = env.get("TAG", ""), env.get("SOURCE_SHA", ""), env.get("TAG_OBJECT_SHA", "")
    version(tag)
    if not SHA.fullmatch(sha) or not SHA.fullmatch(object_sha):
        raise ValueError("Immutable source identity is incomplete")
    source = Path(env["APP_SOURCE_DIR"]).resolve(strict=True)
    if checked(["git", "rev-parse", "HEAD"], cwd=source, capture=True) != sha:
        raise ValueError("Checked-out source is not the approved commit")
    if checked(["git", "rev-parse", "refs/tags/" + tag], cwd=source, capture=True) != object_sha:
        raise ValueError("Source tag moved after identity validation")
    if checked(["git", "describe", "--tags", "--always", "--dirty"], cwd=source, capture=True) != tag:
        raise ValueError("Release source must be clean and describe exactly the selected tag")
    if not re.search(r"(?m)^go 1\.27\.4\s*$", (source / "go.mod").read_text()):
        raise ValueError("Source Go version differs from the pinned release toolchain")
    return source, tag, sha


def build(env, platform):
    source, _, _ = source_identity(env)
    script = source / "scripts" / ("build-release.ps1" if platform == "windows-amd64" else "build-release.sh")
    if script.is_symlink() or not script.is_file() or not (source / "ui/package-lock.json").is_file():
        raise ValueError("The reviewed app build script or locked real UI is missing")
    child = dict(env, GOTOOLCHAIN="local", NPM_CONFIG_ENGINE_STRICT="true")
    # Match a normal shell; this harness-only variable breaks cmd-driven npm scripts.
    child.pop("NoDefaultCurrentDirectoryInExePath", None)
    args = ["pwsh", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)] if platform == "windows-amd64" else ["bash", str(script)]
    checked(args, cwd=source, env=child)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("checkout", "preflight", "build", "check-windows", "sign-macos", "unsigned-macos", "stage", "draft", "upload", "finalize"))
    parser.add_argument("--platform", choices=("windows-amd64", "darwin-arm64"))
    args = parser.parse_args()
    env = dict(os.environ)
    try:
        tag, sha = env.get("TAG", ""), env.get("SOURCE_SHA", "")
        version(tag)
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid source commit")
        unsigned = env.get("UNSIGNED_MACOS_TEST", "false")
        if unsigned not in ("true", "false"):
            raise ValueError("Invalid unsigned test selection")
        unsigned = unsigned == "true"
        if args.command == "checkout":
            from app_release_checkout import checkout
            checkout(env)
        elif args.command == "preflight":
            source_identity(env)
        elif args.command == "build":
            if not args.platform:
                raise ValueError("Platform required")
            build(env, args.platform)
        elif args.command == "check-windows":
            from app_release_windows import check
            check(Path(env["APP_SOURCE_DIR"]) / "dist", tag)
        elif args.command in ("sign-macos", "unsigned-macos"):
            if (args.command == "unsigned-macos") != unsigned:
                raise ValueError("macOS signing mode does not match the reviewed dispatch")
            from app_release_macos import package
            package(Path(env["APP_SOURCE_DIR"]) / "dist", tag, env, unsigned)
        elif args.command == "stage":
            if not args.platform:
                raise ValueError("Platform required")
            prepare(Path(env["APP_SOURCE_DIR"]) / "dist", env["APP_ASSET_DIR"], tag, sha, args.platform, unsigned)
        elif args.command == "draft":
            from app_release_publish import draft
            result = draft(env["APP_RELEASE_REPOSITORY"], env["APP_RELEASE_REPOSITORY_ID"], tag, sha, unsigned, create=True)
            if not isinstance(result.get("id"), int):
                raise ValueError("Invalid draft release identity")
        elif args.command == "upload":
            from app_release_publish import upload
            upload(env["APP_RELEASE_REPOSITORY"], env["APP_RELEASE_REPOSITORY_ID"], tag, sha, env["APP_ASSET_DIR"], args.platform, unsigned)
        else:
            from app_release_publish import finalize
            finalize(env["APP_RELEASE_REPOSITORY"], env["APP_RELEASE_REPOSITORY_ID"], tag, sha, unsigned)
        print("New-app release gate passed")
        return 0
    except Exception:
        print("New-app release gate failed; private values and tool output withheld", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
