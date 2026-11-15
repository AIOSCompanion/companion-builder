#!/usr/bin/env python3
"""Prepare the unprivileged test user that runs the PR's install and tests.

    python3 -I -B scripts/prepare_untrusted.py --user pr-tests --checkout pr

Creates the user if it is missing, and hands the PR checkout to it, so its install and tests can write there.

With --home, it also asks for mode 711 on the runner's home directory: the test user may traverse it (the
workspace is under it) but may not list it. That is attempted, not required. If the image refuses, the run
continues, and the permission probe reports the listing as readable. The runner's gpg keyring is in ~/.gnupg,
which gpg keeps at 0700 regardless.
"""
from __future__ import annotations

import argparse
import subprocess
import sys


def lock_home(home, run=subprocess.run):
    """711: the test user may traverse the runner's home (the workspace is under it) but not list it.

    Not fatal if the image refuses: the permission probe reports the listing state either way.
    """
    if not home:
        return "not requested"
    return "locked 711" if run(["sudo", "-n", "chmod", "711", home], check=False).returncode == 0 \
        else "could not lock (the permission probe reports the listing state)"


def prepare(user, checkout, run=subprocess.run, home=None):
    exists = run(["id", "-u", user], capture_output=True, check=False).returncode == 0
    if not exists:
        if run(["sudo", "-n", "useradd", "--create-home", "--shell", "/bin/bash", user],
               check=False).returncode != 0:
            raise RuntimeError("could not create the test user")
    if run(["sudo", "-n", "chown", "-R", f"{user}:{user}", checkout], check=False).returncode != 0:
        raise RuntimeError("could not hand the PR checkout to the test user")
    return f"{'created' if not exists else 'already present'}; home {lock_home(home, run=run)}"


def main(argv=None, *, run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="pr-tests")
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--home", default=None, help="the runner's home directory to lock to 711")
    args = parser.parse_args(argv)
    try:
        state = prepare(args.user, args.checkout, run=run, home=args.home)
    except RuntimeError as error:
        print(f"prepare_untrusted: {error}", file=sys.stderr)
        return 1
    print(f"prepare_untrusted: test user {state}; checkout handed over")
    return 0


if __name__ == "__main__":
    sys.exit(main())
