"""Bind the new app's private source tag to its configured public destination.

The destination is one builder variable, APP_RELEASE_REPOSITORY ("AIOSCompanion/<name>"), so the owner's
choice of name is a single setting. It is checked twice: here, before any private checkout, as configuration
only (this job's App token is scoped to companion-engine and is not used on the destination); and by every
publishing step, with the publishing credential, against GitHub's own metadata, where today's updater
repository and the builder's other repositories are refused by immutable ID as well as by name.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_release_ref import resolve

SOURCE = "AIOSCompanion/companion-engine"
# Every other repository the organisation had on 17 Nov 2026 (14), by name and by immutable ID, so a rename cannot
# turn one of them into the app's destination. Today's updater feed (aios-companion-releases) comes first; the only
# repository left out is the app's own destination, companion-app-releases (1461828770).
FORBIDDEN_REPOSITORIES = {
    "AIOSCompanion/aios-companion-releases": "1025431750",
    "AIOSCompanion/aios-desktop-app": "1023819429",
    "AIOSCompanion/companion-builder": "1458662787",
    SOURCE: "1461476177",
    "AIOSCompanion/aiosxaegra": "1145765349",
    "AIOSCompanion/companion-memory": "1212522333",
    "AIOSCompanion/AIOSCompanion": "976654857",
    "AIOSCompanion/AIOS_Commercial": "1008300374",
    "AIOSCompanion/aios": "1389705388",
    "AIOSCompanion/aios-langgraph-server": "1022520496",
    "AIOSCompanion/aios-mobile": "1022530766",
    "AIOSCompanion/companion-workspace-recovery": "1414764802",
    "AIOSCompanion/real-ui-showreel": "1431594049",
    "AIOSCompanion/test-repo-check": "1079973351",
}
FORBIDDEN_TARGETS = frozenset(FORBIDDEN_REPOSITORIES)
FORBIDDEN_IDS = frozenset(FORBIDDEN_REPOSITORIES.values())
DESTINATION = re.compile(r"AIOSCompanion/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


def validate_configuration(repository):
    """The owner's configured destination, before any network access. There is no default."""
    if (not DESTINATION.fullmatch(repository or "") or repository.endswith(".git")
            or repository.lower() in {name.lower() for name in FORBIDDEN_TARGETS}):
        raise ValueError("a separate approved public release repository must be configured")
    return repository


def validate_destination(repository, get):
    """GitHub's metadata for the destination, read with the publishing credential. Returns that metadata."""
    validate_configuration(repository)
    metadata = get("repos/" + repository)
    if (metadata.get("full_name") != repository or str(metadata.get("id")) in FORBIDDEN_IDS
            or metadata.get("private") is not False or metadata.get("visibility") != "public"
            or metadata.get("archived") is not False or metadata.get("disabled") is not False
            or (metadata.get("owner") or {}).get("login") != "AIOSCompanion"):
        raise ValueError("release destination identity or visibility changed")
    permissions = metadata.get("permissions")
    if isinstance(permissions, dict) and permissions.get("push") is not True:
        raise ValueError("the publishing credential cannot write to the release destination")
    return metadata


def identity(tag, expected, repository, get):
    target = validate_configuration(repository)
    original = {}
    def read(path):
        result = get(path)
        if path.startswith(f"repos/{SOURCE}/git/ref/tags/"):
            original["sha"] = result["object"]["sha"]
        return result
    sha = resolve(tag, expected, read, repository=SOURCE)
    return {"commit_sha": sha, "tag_object_sha": original["sha"], "release_repository": target}


def github_get(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True, check=False)
    if result.returncode:
        raise ValueError("release metadata could not be read; response withheld")
    return json.loads(result.stdout)


def main():
    # This step runs outside the sealed wrapper, so it prints fixed messages only.
    try:
        if (os.environ.get("GITHUB_REPOSITORY") != "AIOSCompanion/companion-builder"
                or os.environ.get("GITHUB_REF") != "refs/heads/main"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
            raise ValueError("new-app releases require a dispatch from builder main")
        result = identity(os.environ.get("TAG", ""), os.environ.get("EXPECTED_SHA", ""),
            os.environ.get("APP_RELEASE_REPOSITORY", ""), github_get)
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8", newline="\n") as output:
            for key, value in result.items():
                output.write(f"{key}={value}\n")
        print("New-app source tag and release destination configuration verified")
        return 0
    except Exception:
        print("New-app release identity rejected; configuration and response values withheld", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
