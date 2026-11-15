#!/usr/bin/env python3
"""Run one PR test suite: no egress, as the untrusted test user, output sealed, junit counted and sealed.

Reads TEST_COMMAND (the suite's command, from the fixed matrix), SUITE_NAME, LOG_RECIPIENT and SEALED_LOG_DIR
from the environment. Runs in the PR checkout. The command runs inside a network namespace with loopback
only, as the unprivileged test user, with a clean environment, through the trusted wrapper beside this file.

Returns the suite's exit code. A failing suite fails the job, so the status step reports failure.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys

TEST_USER = "pr-tests"


def inner_command(command, user, path):
    """The text run as root inside the namespace: bring loopback up, then become the test user with a clean env."""
    search_path = shlex.quote(f"/home/{user}/.local/bin:{path}")
    env = (f"env -i PATH={search_path} HOME=/home/{user} LANG=C.UTF-8 "
           f"PYTHONDONTWRITEBYTECODE=1")
    return f"ip link set lo up && exec runuser -u {user} -- {env} bash -c {shlex.quote(command)}"


def build_argv(python, wrapper, suite, log_dir, recipient, command, path, user=TEST_USER):
    return [python, "-I", "-B", str(wrapper), "--name", suite, "--log-dir", log_dir, "--recipient", recipient,
            "--junit", "junit.xml", "--", "sudo", "-n", "unshare", "--net", "bash", "-c",
            inner_command(command, user, path)]


def main(argv=None, *, run=subprocess.run, env=None):
    env = dict(os.environ if env is None else env)
    suite = env.get("SUITE_NAME", "")
    command = env.get("TEST_COMMAND", "")
    recipient = env.get("LOG_RECIPIENT", "")
    log_dir = env.get("SEALED_LOG_DIR", "")
    if not (suite and command and recipient and log_dir):
        print("run_suite: SUITE_NAME, TEST_COMMAND, LOG_RECIPIENT and SEALED_LOG_DIR are all required", flush=True)
        return 2
    wrapper = Path(__file__).resolve().parent / "run_sealed.py"
    argv_list = build_argv(sys.executable, wrapper, suite, log_dir, recipient, command, env.get("PATH", ""))
    return run(argv_list, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
