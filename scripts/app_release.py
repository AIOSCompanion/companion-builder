"""Trusted entry points for the new app. Every command runs inside run_sealed.py.

Their output, including the cause of a failure and the tools' own output, goes only into the log sealed to
the team key; the public log shows the step's exit code, duration and error class. A failure therefore says
why in the decrypted log, which is what the owner needs to recover a release.
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_release_artifacts import SHA, prepare, version
import app_release_toolchain as toolchain


def checked(args, *, cwd=None, env=None, capture=False, timeout=1800):
    result = subprocess.run(args, cwd=cwd, env=env, capture_output=capture, text=True, check=False, timeout=timeout)
    if result.returncode:
        if capture:
            print((result.stdout or "") + (result.stderr or ""))
        raise RuntimeError(f"{Path(args[0]).name} {' '.join(args[1:3])} failed with exit {result.returncode}")
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
    return source, tag, sha


def check_toolchain(source):
    """The installed Go is exactly go.mod's, and the installed Node satisfies the UI's engines."""
    pinned = toolchain.go_version(toolchain.read(source, "go.mod"))
    installed = checked(["go", "env", "GOVERSION"], cwd=source, capture=True)
    if installed != "go" + pinned:
        raise ValueError(f"Installed Go {installed} is not go.mod's go{pinned}")
    spec = toolchain.node_spec(toolchain.read(source, "ui/package.json"))
    node = checked(["node", "--version"], cwd=source, capture=True)
    if not toolchain.node_satisfies(spec, node):
        raise ValueError(f"Installed Node {node} does not satisfy the UI's engines.node")
    print(f"Toolchain matches the pinned source: go{pinned}, Node {node}")


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


def write_outputs(values):
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("checkout", "checkout-resources", "toolchain", "preflight", "build", "resources", "check-windows", "check-macos",
                                            "sign-macos", "unsigned-macos", "stage", "draft", "upload", "finalize"))
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
        if args.command in ("checkout", "checkout-resources"):
            from app_release_checkout import checkout
            checkout(env, resources=args.command == "checkout-resources")
        elif args.command == "toolchain":
            values = toolchain.resolve(Path(env["APP_SOURCE_DIR"]))
            write_outputs(values)
            print(f"Pinned source asks for Go {values['go_version']} and Node {values['node_version']}")
        elif args.command == "preflight":
            source, _, _ = source_identity(env)
            check_toolchain(source)
        elif args.command == "build":
            if not args.platform:
                raise ValueError("Platform required")
            build(env, args.platform)
        elif args.command == "resources":
            if not args.platform:
                raise ValueError("Platform required")
            source, _, _ = source_identity(env)
            from app_release_resources import ResourceStageError, stage_resources
            try:
                stage_resources(source, env, tag, args.platform)
            except ResourceStageError as error:
                traceback.print_exc()
                return error.status
        elif args.command == "check-windows":
            from app_release_windows import check
            check(Path(env["APP_SOURCE_DIR"]) / "dist", tag)
        elif args.command == "check-macos":
            # Its own step, without signing material: the only place the built app runs before it is signed.
            from app_release_macos import check
            check(Path(env["APP_SOURCE_DIR"]) / "dist", tag)
        elif args.command in ("sign-macos", "unsigned-macos"):
            if (args.command == "unsigned-macos") != unsigned:
                raise ValueError("macOS signing mode does not match the reviewed dispatch")
            from app_release_macos import package
            package(Path(env["APP_SOURCE_DIR"]) / "dist", tag, env, unsigned)
        elif args.command == "stage":
            if not args.platform:
                raise ValueError("Platform required")
            dist = Path(env["APP_SOURCE_DIR"]) / "dist"
            if args.platform == "darwin-arm64" and not unsigned:
                # Independent of the signing step: no step order can stage a macOS ZIP for a signed draft
                # unless the app in it is Developer ID signed, notarised and stapled.
                from app_release_macos import verify_signed_archive
                verify_signed_archive(dist, tag)
            prepare(dist, env["APP_ASSET_DIR"], tag, sha, args.platform, unsigned)
        elif args.command == "draft":
            from app_release_publish import draft
            draft(env["APP_RELEASE_REPOSITORY"], tag, sha, unsigned, create=True)
        elif args.command == "upload":
            from app_release_publish import upload
            upload(env["APP_RELEASE_REPOSITORY"], tag, sha, env["APP_ASSET_DIR"], args.platform, unsigned)
        else:
            from app_release_publish import finalize
            finalize(env["APP_RELEASE_REPOSITORY"], tag, sha, unsigned)
        print("New-app release gate passed")
        return 0
    except Exception as error:
        # Sealed output: the cause is for the owner's decrypted log. No secret is part of any message.
        traceback.print_exc()
        print(f"New-app release gate failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
