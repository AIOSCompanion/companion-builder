"""Bind the new app's private source tag to its explicitly configured public destination."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_release_ref import resolve

SOURCE = "AIOSCompanion/companion-engine"
FORBIDDEN_TARGETS = frozenset((SOURCE, "AIOSCompanion/aios-companion-releases",
    "AIOSCompanion/aios-desktop-app", "AIOSCompanion/companion-builder",
    "AIOSCompanion/aiosxaegra", "AIOSCompanion/companion-memory"))
FORBIDDEN_IDS = frozenset(("1025431750", "1458662787", "1461476177"))  # old updater, builder, source


def validate_destination(repository, repository_id, get):
    """The owner configures both name and immutable ID; there is no default feed."""
    if (not re.fullmatch(r"AIOSCompanion/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repository or "")
            or repository in FORBIDDEN_TARGETS
            or not re.fullmatch(r"[1-9][0-9]{0,19}", repository_id or "") or repository_id in FORBIDDEN_IDS):
        raise ValueError("a separate approved public release repository must be configured")
    metadata = get("repos/" + repository)
    if (metadata.get("full_name") != repository or str(metadata.get("id")) != repository_id
            or metadata.get("private") is not False or metadata.get("visibility") != "public"
            or metadata.get("archived") is not False or metadata.get("disabled") is not False
            or (metadata.get("owner") or {}).get("login") != "AIOSCompanion"):
        raise ValueError("release destination identity or visibility changed")
    return repository


def identity(tag, expected, repository, repository_id, get):
    target = validate_destination(repository, repository_id, get)
    original = {}
    def read(path):
        result = get(path)
        if path.startswith(f"repos/{SOURCE}/git/ref/tags/"):
            original["sha"] = result["object"]["sha"]
        return result
    sha = resolve(tag, expected, read, repository=SOURCE)
    return {"commit_sha": sha, "tag_object_sha": original["sha"],
            "release_repository": target, "release_repository_id": repository_id}


def github_get(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("release metadata could not be read; response withheld")
    return json.loads(result.stdout)


def main():
    try:
        if (os.environ.get("GITHUB_REPOSITORY") != "AIOSCompanion/companion-builder"
                or os.environ.get("GITHUB_REF") != "refs/heads/main"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
            raise ValueError("new-app releases require a dispatch from builder main")
        result = identity(os.environ.get("TAG", ""), os.environ.get("EXPECTED_SHA", ""),
            os.environ.get("APP_RELEASE_REPOSITORY", ""), os.environ.get("APP_RELEASE_REPOSITORY_ID", ""), github_get)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
        print("New-app source tag and separate public release destination verified")
        return 0
    except Exception:
        print("New-app release identity rejected; configuration and response values withheld", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
