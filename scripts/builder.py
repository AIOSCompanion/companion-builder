"""The entry point for Companion CI and releases. Dispatches only this public builder.

doctor lists missing configuration names, never secret values. test/release refuse to dispatch until
their configuration is present. self-check exercises public builder code without private credentials.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

BUILDER = "AIOSCompanion/companion-builder"
REPOSITORIES = ("aios-desktop-app", "aiosxaegra", "companion-memory", "companion-engine")
RELEASE_SECRETS = {"APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER", "CSC_LINK",
                   "CSC_KEY_PASSWORD", "DOTENV_PRODUCTION_B64", "SENTRY_AUTH_TOKEN",
                   "PUBLISH_RELEASES_TOKEN"}
# The Go/Wails app (release-app.yml): no Electron build inputs. An explicit unsigned macOS test build needs no
# signing material; the destination is the builder variable APP_RELEASE_REPOSITORY.
APP_SIGNING_SECRETS = {"APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER", "CSC_LINK", "CSC_KEY_PASSWORD"}
APP_RELEASE_SECRETS = {"PUBLISH_RELEASES_TOKEN"}
TAG = r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?"


def environment_for(mode):
    return "source-access" if mode == "test" else "release-signing"


def api(path, run=subprocess.run):
    result = run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("configuration could not be inspected; no workflow was dispatched")
    return json.loads(result.stdout)


def missing_configuration(mode, variables, secrets, environment=None, policies=(), environment_secrets=(),
                          unsigned_macos=False):
    required_variables = ["BUILDER_APP_ID", "TEST_LOG_PUBLIC_KEY", "TEST_LOG_RECIPIENT"]
    if mode == "release-app":
        required_variables.append("APP_RELEASE_REPOSITORY")
    missing = [f"variable:{key}" for key in required_variables if key not in variables]
    if "BUILDER_APP_PRIVATE_KEY" in secrets:
        missing.append("unsafe-repository-secret:BUILDER_APP_PRIVATE_KEY")
    name = environment_for(mode)
    if environment is None:
        missing.append(f"environment:{name}")
    else:
        if not any(rule.get("type") == "required_reviewers" and rule.get("reviewers")
                   for rule in environment.get("protection_rules", [])):
            missing.append("environment:required-reviewer")
        policy = environment.get("deployment_branch_policy") or {}
        if not policy.get("custom_branch_policies") or policy.get("protected_branches"):
            missing.append("environment:main-only-policy")
        if list(policies) != [{"name": "main", "type": "branch"}]:
            missing.append("environment:main-only-branch-rule")
    if "BUILDER_APP_PRIVATE_KEY" not in environment_secrets:
        missing.append(f"environment-secret:{name}/BUILDER_APP_PRIVATE_KEY")
    if mode == "release":
        missing.extend(f"environment-secret:{name}" for name in sorted(RELEASE_SECRETS - set(environment_secrets)))
    elif mode == "release-app":
        required = APP_RELEASE_SECRETS | (set() if unsigned_macos else APP_SIGNING_SECRETS)
        missing.extend(f"environment-secret:{name}" for name in sorted(required - set(environment_secrets)))
    return missing


def doctor(mode, get=api, unsigned_macos=False):
    root = f"repos/{BUILDER}"
    variables = {v["name"] for v in get(root + "/actions/variables?per_page=100")["variables"] if v.get("value")}
    secrets = {s["name"] for s in get(root + "/actions/secrets?per_page=100")["secrets"]}
    environment = None
    policies, env_secrets = [], []
    name = environment_for(mode)
    environments = get(root + "/environments")["environments"]
    environment = next((e for e in environments if e["name"] == name), None)
    if environment is not None:
        environment = get(root + f"/environments/{name}")
        policies = [{"name": p["name"], "type": p["type"]} for p in
                    get(root + f"/environments/{name}/deployment-branch-policies")["branch_policies"]]
        env_secrets = [s["name"] for s in get(root + f"/environments/{name}/secrets?per_page=100")["secrets"]]
    return missing_configuration(mode, variables, secrets, environment, policies, env_secrets,
                                 unsigned_macos=unsigned_macos)


def dispatch_args(command, repository=None, sha=None, tag=None, *, memory_sha=None, engine_sha=None, desktop_sha=None, diagnostic=False,
                  allow_unsigned_macos_test=False):
    if command == "test":
        if repository not in REPOSITORIES or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise ValueError("test requires an allowed repository and a full lowercase commit SHA")
        workflow, inputs = "test.yml", ["-f", f"repository={repository}", "-f", f"head_sha={sha}"]
        if diagnostic:
            inputs.extend(["-f", "diagnostic=true"])
        for name, value, allowed in [("memory_sha", memory_sha, {"aiosxaegra"}), ("engine_sha", engine_sha, {"companion-memory", "aios-desktop-app"}), ("desktop_sha", desktop_sha, {"aiosxaegra"})]:
            if value is not None:
                if repository not in allowed or not re.fullmatch(r"[0-9a-f]{40}", value):
                    raise ValueError(f"{name} requires its matching suite and a full lowercase commit SHA")
                inputs.extend(["-f", f"{name}={value}"])
    elif command == "release":
        if not re.fullmatch(TAG, tag or ""):
            raise ValueError("release requires a version tag, not a branch")
        workflow, inputs = "release.yml", ["-f", f"tag={tag}"]
    elif command == "release-app":
        if not re.fullmatch(TAG, tag or ""):
            raise ValueError("release-app requires a companion-engine version tag, not a branch")
        workflow, inputs = "release-app.yml", ["-f", f"tag={tag}", "-f",
                                               f"allow_unsigned_macos_test={'true' if allow_unsigned_macos_test else 'false'}"]
    else:
        workflow, inputs = "self-check.yml", []
    return ["gh", "workflow", "run", workflow, "--repo", BUILDER, "--ref", "main", *inputs]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor")
    check.add_argument("--mode", choices=("test", "release", "release-app"), default="release")
    check.add_argument("--allow-unsigned-macos-test", action="store_true",
                       help="release-app: check for an explicitly unsigned macOS test build (no signing material)")
    tests = commands.add_parser("test")
    tests.add_argument("repository", choices=REPOSITORIES)
    tests.add_argument("sha")
    tests.add_argument("--memory-sha")
    tests.add_argument("--engine-sha")
    tests.add_argument("--desktop-sha")
    tests.add_argument("--diagnostic", action="store_true")
    release = commands.add_parser("release")
    release.add_argument("tag")
    app_release = commands.add_parser("release-app", help="the Go/Wails app from a companion-engine version tag")
    app_release.add_argument("tag")
    app_release.add_argument("--allow-unsigned-macos-test", action="store_true",
                             help="build a labelled UNSIGNED macOS test draft instead of signing and notarising")
    commands.add_parser("self-check")
    args = parser.parse_args(argv)
    unsigned_macos = getattr(args, "allow_unsigned_macos_test", False)
    try:
        if args.command == "doctor":
            missing = doctor(args.mode, unsigned_macos=unsigned_macos)
            print(json.dumps({"ready": not missing, "mode": args.mode, "missing": missing}, indent=2))
            return 2 if missing else 0
        command = dispatch_args(args.command, getattr(args, "repository", None), getattr(args, "sha", None),
                                getattr(args, "tag", None), memory_sha=getattr(args, "memory_sha", None),
                                engine_sha=getattr(args, "engine_sha", None), desktop_sha=getattr(args, "desktop_sha", None), diagnostic=getattr(args, "diagnostic", False),
                                allow_unsigned_macos_test=unsigned_macos)
        if args.command != "self-check":
            missing = doctor(args.command, unsigned_macos=unsigned_macos)
            if missing:
                print(json.dumps({"dispatched": False, "missing": missing}, indent=2))
                return 2
        return subprocess.run(command, check=False).returncode
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"builder: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
