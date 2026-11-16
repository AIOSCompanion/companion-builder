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
import re
from pathlib import Path
import sys

SUITES = {
    "aiosxaegra": [
        {"suite": "engine unit tests (pytest)", "repo": "aiosxaegra",
         "setup": "pip install --quiet --user uv && uv sync --locked --extra voice-worker && uv export --locked --extra voice-worker --no-hashes --no-emit-project > /tmp/engine-constraints.txt && uv pip install --python .venv/bin/python --constraint /tmp/engine-constraints.txt ../companion-memory",
         "run": ".venv/bin/python -m pytest tests/unit -vv --maxfail=1 -o faulthandler_timeout=120 --tb=short -p no:cacheprovider --junitxml=junit.xml"},
    ],
    "companion-memory": [
        {"suite": "memory unit tests (pytest)", "repo": "companion-memory",
         "setup": "pip install --quiet --user uv && uv sync --project ../aiosxaegra --locked --extra voice-worker && uv export --project ../aiosxaegra --locked --extra voice-worker --no-hashes --no-emit-project > /tmp/engine-constraints.txt && uv pip install --python ../aiosxaegra/.venv/bin/python --constraint /tmp/engine-constraints.txt --editable '.[dev]' && ln -s ../aiosxaegra/.venv .venv",
         "run": ".venv/bin/python -m pytest -q --tb=no -p no:cacheprovider --junitxml=junit.xml"},
    ],
    "aios-desktop-app": [
        {"suite": "desktop unit tests (vitest)", "repo": "aios-desktop-app",
         # RobotJS 0.6 calls strdup without including string.h. Modern GCC rejects
         # the missing prototype; provide the real declaration, never suppress it.
         "setup": "CFLAGS='-include string.h -D_DEFAULT_SOURCE' npm ci --no-audit --no-fund", "run": "npx vitest run --reporter=junit --outputFile=junit.xml"},
        {"suite": "desktop typecheck", "repo": "aios-desktop-app",
         "setup": "npm ci --ignore-scripts", "run": "npm run typecheck"},
    ],
}

# Native-host/PTY tests are intentionally slow. Every shard collects the complete
# suite, then hashes node IDs into a disjoint partition; no tests are dropped.
_engine = SUITES['aiosxaegra'][0]
SUITES['aiosxaegra'] = [dict(_engine, **{
    'suite': f'engine unit tests shard {index + 1}/8 (pytest)',
    'run': f'.venv/bin/python @TRUSTED_SHARD_RUNNER@ --index {index} --count 8 -- '
           'tests/unit -vv -o faulthandler_timeout=120 --tb=short '
           '-p no:cacheprovider --junitxml=junit.xml',
}) for index in range(8)]


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
    parser.add_argument("--head-sha", default=env.get("HEAD_SHA"))
    args = parser.parse_args(argv)
    try:
        if args.head_sha is not None and not re.fullmatch(r"[0-9a-f]{40}", args.head_sha):
            raise PlanError("head SHA must be an immutable 40-character commit")
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
