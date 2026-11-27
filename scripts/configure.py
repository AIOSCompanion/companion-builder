"""Populate builder configuration from explicitly supplied local files without logging secret values.

No credential search or GitHub secret read-back. Run this on the owner's machine with original key/config files.
The optional release-files JSON maps environment-secret names to local text files containing their intended values.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from builder import BUILDER, RELEASE_SECRETS


def set_value(kind, name, value, *, environment=None, run=subprocess.run):
    args = ["gh", kind, "set", name, "--repo", BUILDER]
    if environment:
        args.extend(["--env", environment])
    # Secret values go through stdin, never a command argument or output.
    if kind == "variable":
        args.extend(["--body", value])
        value = None
    result = run(args, input=value, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"could not configure {kind} {name}; no value was printed")


# Every environment that reads private source holds its own copy of the reader app key, and each requires a
# reviewer and runs only from main: tests (source-access), releases (release-signing), and the relay and portal
# deploys, whose jobs mint the read token themselves because GitHub drops a secret job output.
APP_KEY_ENVIRONMENTS = ("source-access", "release-signing", "relay-deploy", "portal-deploy")


def set_app_key(value, *, run=subprocess.run):
    for environment in APP_KEY_ENVIRONMENTS:
        set_value("secret", "BUILDER_APP_PRIVATE_KEY", value, environment=environment, run=run)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, type=int)
    parser.add_argument("--app-key-file", required=True, type=Path)
    parser.add_argument("--log-public-key-file", required=True, type=Path)
    parser.add_argument("--log-recipient", required=True)
    parser.add_argument("--release-files", type=Path)
    args = parser.parse_args(argv)
    try:
        # Load and validate every named file before the first mutation.
        app_key = args.app_key_file.read_text(encoding="utf-8")
        public_key = args.log_public_key_file.read_text(encoding="utf-8")
        if args.app_id <= 0 or not app_key.strip() or "BEGIN PGP PUBLIC KEY BLOCK" not in public_key:
            raise ValueError("invalid app id, empty app key or missing public log key")
        release = {}
        if args.release_files:
            mapping = json.loads(args.release_files.read_text(encoding="utf-8"))
            if not isinstance(mapping, dict) or set(mapping) - RELEASE_SECRETS:
                raise ValueError("release file map contains an unknown secret name")
            release = {name: Path(path).read_text(encoding="utf-8") for name, path in mapping.items()}
            if any(not value.strip() for value in release.values()):
                raise ValueError("a release secret file is empty")
        set_value("variable", "BUILDER_APP_ID", str(args.app_id))
        set_value("variable", "TEST_LOG_PUBLIC_KEY", public_key)
        set_value("variable", "TEST_LOG_RECIPIENT", args.log_recipient)
        set_app_key(app_key)
        for name, value in release.items():
            set_value("secret", name, value, environment="release-signing")
        print("Builder configuration uploaded. Run builder.py doctor to check remaining names; no values were printed.")
        return 0
    except (OSError, ValueError, RuntimeError):
        print("Builder configuration failed; verify the explicitly supplied files and GitHub access. No values were printed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
