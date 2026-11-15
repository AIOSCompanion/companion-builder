#!/usr/bin/env python3
"""Post companion-builder/tests on the private PR head commit.

Reads REPOSITORY, SHA, RUN_URL, NEEDS_RESULT and GH_TOKEN (the status-only App token, created in this step
alone). Refuses a repository outside the three private repos or a SHA that is not 40 hex characters.
The description carries no test name and no output.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

REPOSITORIES = ("aios-desktop-app", "aiosxaegra", "companion-memory")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class StatusError(Exception):
    pass


def state_for(result):
    return "success" if result == "success" else "failure"


def gh_args(repository, sha, result, run_url):
    if repository not in REPOSITORIES:
        raise StatusError("unknown repository")
    if not SHA_RE.match(sha):
        raise StatusError("head SHA must be 40 lowercase hex characters")
    return ["gh", "api", f"repos/AIOSCompanion/{repository}/statuses/{sha}",
            "-f", f"state={state_for(result)}", "-f", "context=companion-builder/tests",
            "-f", f"target_url={run_url}", "-f", f"description=Tests {result}; logs are sealed, see the run"]


def main(argv=None, *, run=subprocess.run, env=None):
    env = os.environ if env is None else env
    try:
        args = gh_args(env.get("REPOSITORY", ""), env.get("SHA", ""), env.get("NEEDS_RESULT", "failure"),
                       env.get("RUN_URL", ""))
    except StatusError as error:
        print(f"post_status: {error}", file=sys.stderr)
        return 2
    result = run(args, env=env, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode != 0:
        print("post_status: the status could not be posted", file=sys.stderr)
        return 1
    print(f"post_status: posted {state_for(env.get('NEEDS_RESULT', 'failure'))} on the head commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
