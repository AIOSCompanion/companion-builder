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
REPOSITORIES = ("aios-desktop-app", "aiosxaegra", "companion-memory")
RELEASE_SECRETS = {"APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER", "CSC_LINK",
                   "CSC_KEY_PASSWORD", "DOTENV_PRODUCTION_B64", "SENTRY_AUTH_TOKEN",
                   "PUBLISH_RELEASES_TOKEN"}


def api(path, run=subprocess.run):
    result = run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("configuration could not be inspected; no workflow was dispatched")
    return json.loads(result.stdout)


def missing_configuration(mode, variables, secrets, environment=None, policies=(), environment_secrets=()):
    missing = [f"variable:{key}" for key in ("BUILDER_APP_ID", "TEST_LOG_PUBLIC_KEY", "TEST_LOG_RECIPIENT")
               if key not in variables]
    if "BUILDER_APP_PRIVATE_KEY" in secrets:
        missing.append("unsafe-repository-secret:BUILDER_APP_PRIVATE_KEY")
    name = "release-signing" if mode == "release" else "source-access"
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
    return missing


def doctor(mode, get=api):
    root = f"repos/{BUILDER}"
    variables = {v["name"] for v in get(root + "/actions/variables?per_page=100")["variables"] if v.get("value")}
    secrets = {s["name"] for s in get(root + "/actions/secrets?per_page=100")["secrets"]}
    environment = None
    policies, env_secrets = [], []
    name = "release-signing" if mode == "release" else "source-access"
    environments = get(root + "/environments")["environments"]
    environment = next((e for e in environments if e["name"] == name), None)
    if environment is not None:
        environment = get(root + f"/environments/{name}")
        policies = [{"name": p["name"], "type": p["type"]} for p in
                    get(root + f"/environments/{name}/deployment-branch-policies")["branch_policies"]]
        env_secrets = [s["name"] for s in get(root + f"/environments/{name}/secrets?per_page=100")["secrets"]]
    return missing_configuration(mode, variables, secrets, environment, policies, env_secrets)


def dispatch_args(command, repository=None, sha=None, tag=None, *, memory_sha=None, engine_sha=None):
    if command == "test":
        if repository not in REPOSITORIES or not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
            raise ValueError("test requires an allowed repository and a full lowercase commit SHA")
        workflow, inputs = "test.yml", ["-f", f"repository={repository}", "-f", f"head_sha={sha}"]
        for name, value, allowed in [("memory_sha", memory_sha, "aiosxaegra"), ("engine_sha", engine_sha, "companion-memory")]:
            if value is not None:
                if repository != allowed or not re.fullmatch(r"[0-9a-f]{40}", value):
                    raise ValueError(f"{name} requires its matching suite and a full lowercase commit SHA")
                inputs.extend(["-f", f"{name}={value}"])
    elif command == "release":
        if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", tag or ""):
            raise ValueError("release requires a version tag, not a branch")
        workflow, inputs = "release.yml", ["-f", f"tag={tag}"]
    else:
        workflow, inputs = "self-check.yml", []
    return ["gh", "workflow", "run", workflow, "--repo", BUILDER, "--ref", "main", *inputs]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("doctor")
    check.add_argument("--mode", choices=("test", "release"), default="release")
    tests = commands.add_parser("test")
    tests.add_argument("repository", choices=REPOSITORIES)
    tests.add_argument("sha")
    tests.add_argument("--memory-sha")
    tests.add_argument("--engine-sha")
    release = commands.add_parser("release")
    release.add_argument("tag")
    commands.add_parser("self-check")
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            missing = doctor(args.mode)
            print(json.dumps({"ready": not missing, "mode": args.mode, "missing": missing}, indent=2))
            return 2 if missing else 0
        command = dispatch_args(args.command, getattr(args, "repository", None), getattr(args, "sha", None),
                                getattr(args, "tag", None), memory_sha=getattr(args, "memory_sha", None),
                                engine_sha=getattr(args, "engine_sha", None))
        if args.command != "self-check":
            missing = doctor(args.command)
            if missing:
                print(json.dumps({"dispatched": False, "missing": missing}, indent=2))
                return 2
        return subprocess.run(command, check=False).returncode
    except (RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"builder: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
