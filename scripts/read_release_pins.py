"""Read immutable sidecar pins from the checked-out private release, never from builder constants.

The input workflow is data, not executable configuration. Refuse missing, moving or conflicting pins.
Only the two validated SHA outputs are printed; no source text is logged.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

import yaml

SHA = re.compile(r"[0-9a-f]{40}\Z")
KEYS = {"EXPECTED_AEGRA_SHA": "aegra_sha", "EXPECTED_MEMORY_SHA": "memory_sha"}


def read_pins(document):
    found = {key: set() for key in KEYS}

    def visit(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in found:
                    if not isinstance(child, str) or not SHA.fullmatch(child):
                        raise ValueError("release pins must be immutable full commit SHAs")
                    found[key].add(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    if any(len(values) != 1 for values in found.values()):
        raise ValueError("release pins are missing or conflict")
    return {KEYS[key]: next(iter(values)) for key, values in found.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True)
    args = parser.parse_args(argv)
    try:
        pins = read_pins(yaml.safe_load(Path(args.workflow).read_text(encoding="utf-8")))
    except (OSError, ValueError, yaml.YAMLError):
        print("release pins unavailable: expected one immutable pin per sidecar", file=sys.stderr)
        return 2
    lines = "".join(f"{key}={value}\n" for key, value in pins.items())
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8", newline="\n") as output:
            output.write(lines)
    else:
        print(lines, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
