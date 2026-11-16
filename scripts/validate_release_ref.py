"""Resolve a version tag to a commit before checkout; reject moved webhook refs.

Only immutable hashes reach workflow outputs. GitHub error bodies and private
repository metadata are never printed.
"""
import json
import os
import re
import subprocess
import sys

SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def resolve(tag, expected, get):
    if not VERSION.fullmatch(tag) or (expected and not SHA.fullmatch(expected)):
        raise ValueError("invalid release identity")
    root = "repos/AIOSCompanion/aios-desktop-app/git"
    obj = get(f"{root}/ref/tags/{tag}")["object"]
    if not SHA.fullmatch(obj.get("sha", "")) or (expected and obj["sha"] != expected):
        raise ValueError("release tag changed")
    for _ in range(10):
        if obj.get("type") == "commit":
            return obj["sha"]
        if obj.get("type") != "tag":
            raise ValueError("tag does not identify a commit")
        obj = get(f"{root}/tags/{obj['sha']}")["object"]
        if not SHA.fullmatch(obj.get("sha", "")):
            raise ValueError("invalid tag object")
    raise ValueError("tag nesting exceeds limit")


def get(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("release ref could not be read")
    return json.loads(result.stdout)


def main():
    try:
        sha = resolve(os.environ.get("TAG", ""), os.environ.get("EXPECTED_SHA", ""), get)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"commit_sha={sha}\n")
        print("Release tag resolved to an immutable commit")
        return 0
    except (ValueError, KeyError, OSError):
        print("Release identity validation failed; no private checkout was performed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
