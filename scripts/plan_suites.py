#!/usr/bin/env python3
"""Choose the PR test suites for one private repository.

The matrix is fixed in this file. The input only selects a row, so a dispatch cannot add a command.
Writes matrix=<json> to GITHUB_OUTPUT, or prints it when run locally.

    python3 -B scripts/plan_suites.py --repository aiosxaegra
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

SUITES = {
    "aiosxaegra": [
        {"suite": "engine unit tests (pytest)", "repo": "aiosxaegra",
         "setup": "pip install --quiet --user uv && uv sync --locked --extra voice-worker",
         "run": "uv run --extra voice-worker pytest tests/unit -q --tb=no -p no:cacheprovider --junitxml=junit.xml"},
    ],
    "companion-memory": [
        {"suite": "memory unit tests (pytest)", "repo": "companion-memory",
         "setup": "pip install --quiet --user uv && uv sync --locked",
         "run": "python -m pytest -q --tb=no -p no:cacheprovider --junitxml=junit.xml"},
    ],
    "aios-desktop-app": [
        {"suite": "desktop unit tests (vitest)", "repo": "aios-desktop-app",
         "setup": "npm ci --ignore-scripts", "run": "npx vitest run --reporter=junit --outputFile=junit.xml"},
        {"suite": "desktop typecheck", "repo": "aios-desktop-app",
         "setup": "npm ci --ignore-scripts", "run": "npm run typecheck"},
    ],
}


class PlanError(Exception):
    pass


def matrix_for(repository):
    if repository not in SUITES:
        raise PlanError("unknown repository")
    return json.dumps(SUITES[repository], separators=(",", ":"))


def main(argv=None, *, env=None):
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", default=env.get("REPOSITORY", ""))
    args = parser.parse_args(argv)
    try:
        line = f"matrix={matrix_for(args.repository)}"
    except PlanError as error:
        print(f"plan_suites: {error}", file=sys.stderr)
        return 1
    target = env.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(line + "\n")
    else:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
