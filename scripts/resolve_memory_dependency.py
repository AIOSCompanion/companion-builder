"""Resolve a fixed companion-memory/engine test peer before untrusted code runs."""
import os
import re
import subprocess


def resolve(pinned, run=subprocess.run, *, repository="companion-memory"):
    if repository not in {"companion-memory", "aiosxaegra", "aios-desktop-app"}:
        raise ValueError("unsupported test dependency repository")
    if pinned:
        if not re.fullmatch(r"[0-9a-f]{40}", pinned):
            raise ValueError("memory dependency must be a full commit SHA")
        return pinned
    result = run(["gh", "api", f"repos/AIOSCompanion/{repository}/commits/main", "--jq", ".sha"],
                 capture_output=True, text=True, check=True)
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("memory dependency resolution returned an invalid SHA")
    return sha


if __name__ == "__main__":
    sha = resolve(os.getenv("MEMORY_SHA", ""), repository=os.getenv("DEPENDENCY_REPOSITORY", "companion-memory"))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"sha={sha}\n")
