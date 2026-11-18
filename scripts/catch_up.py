"""Expand truncated push deliveries after approval; never emit private API bodies."""
import json
import os
import re
import subprocess
import sys

REPOSITORIES = ("aios-desktop-app", "aiosxaegra", "companion-memory", "companion-engine")
SHA = re.compile(r"[0-9a-f]{40}")
ZERO = "0" * 40


def expand(repository, before, head, get, send):
    if repository not in REPOSITORIES or not SHA.fullmatch(before) or not SHA.fullmatch(head) or head == ZERO:
        raise ValueError("invalid source range")
    seen = set()
    page = 1
    while True:
        root = f"repos/AIOSCompanion/{repository}"
        path = f"{root}/commits?sha={head}&per_page=100&page={page}" if before == ZERO else (
            f"{root}/compare/{before}...{head}?per_page=100&page={page}")
        response = get(path)
        rows = response if isinstance(response, list) else response["commits"]
        if not isinstance(rows, list) or any(not SHA.fullmatch(row.get("sha", "")) for row in rows):
            raise ValueError("invalid commit page")
        for row in rows:
            if row["sha"] not in seen:
                send(repository, row["sha"])
                seen.add(row["sha"])
        if len(rows) < 100:
            break
        page += 1
    if head not in seen:
        send(repository, head)
        seen.add(head)
    return len(seen)


def call(path, token, payload=None):
    args = ["gh", "api", path]
    if payload is not None:
        args += ["--method", "POST", "--input", "-"]
    env = dict(os.environ)
    # Each API call receives only its own token; neither token enters argv.
    env.pop("SOURCE_TOKEN", None)
    env.pop("DISPATCH_TOKEN", None)
    env["GH_TOKEN"] = token
    result = subprocess.run(args, input=json.dumps(payload) if payload is not None else None,
                            env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError("GitHub request failed; private response withheld")
    return json.loads(result.stdout) if result.stdout.strip() else None


def main():
    try:
        def send(repo, sha):
            call("repos/AIOSCompanion/companion-builder/actions/workflows/test.yml/dispatches",
                 os.environ["DISPATCH_TOKEN"], {"ref": "main", "inputs": {"repository": repo, "head_sha": sha}})
        count = expand(os.environ["REPOSITORY"], os.environ["BEFORE_SHA"], os.environ["HEAD_SHA"],
                       lambda path: call(path, os.environ["SOURCE_TOKEN"]), send)
        print(f"Dispatched {count} immutable commit tests through the public builder")
        return 0
    except (ValueError, KeyError, TypeError, RuntimeError):
        print("Catch-up failed; rerun the approved job. No private response was printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
