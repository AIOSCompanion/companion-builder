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
    """Run with a clean env, then reap this job's dedicated test UID.

    Detached test helpers can outlive pytest and keep its output pipe open.
    The root trap also reaches those escaped process groups before sealing.
    """
    quoted_user = shlex.quote(user)
    search_path = shlex.quote(f"/home/{user}/.local/bin:{path}")
    env = (f"env -i PATH={search_path} HOME=/home/{user} LANG=C.UTF-8 "
           f'PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD:$PWD/graphs"')
    cleanup = (f"status=$?; /usr/bin/pkill -KILL -u {quoted_user} >/dev/null 2>&1; "
               'cleanup_status=$?; if [ "$cleanup_status" -gt 1 ]; then exit 125; fi; exit "$status"')
    return (f"trap {shlex.quote(cleanup)} EXIT; trap 'exit 143' TERM; "
            f"ip link set lo up && runuser -u {quoted_user} -- {env} bash -c {shlex.quote(command)}")


def build_argv(python, wrapper, suite, log_dir, recipient, command, path, user=TEST_USER, timeout_seconds=3300):
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 3300:
        raise ValueError("suite deadline must be between 1 and 3300 seconds")
    return [python, "-I", "-B", str(wrapper), "--name", suite, "--log-dir", log_dir, "--recipient", recipient,
            "--junit", "junit.xml", "--", "sudo", "-n", "/usr/bin/timeout", "--signal=TERM", "--kill-after=30s",
            f"{timeout_seconds}s", "unshare", "--net", "bash", "-c",
            inner_command(command, user, path)]


def main(argv=None, *, run=subprocess.run, env=None):
    env = dict(os.environ if env is None else env)
    suite = env.get("SUITE_NAME", "")
    command = env.get("TEST_COMMAND", "")
    command = command.replace('@TRUSTED_SHARD_RUNNER@',
                              shlex.quote(str(Path(__file__).resolve().parent / 'shard_pytest.py')))
    recipient = env.get("LOG_RECIPIENT", "")
    log_dir = env.get("SEALED_LOG_DIR", "")
    if not (suite and command and recipient and log_dir):
        print("run_suite: SUITE_NAME, TEST_COMMAND, LOG_RECIPIENT and SEALED_LOG_DIR are all required", flush=True)
        return 2
    wrapper = Path(__file__).resolve().parent / "run_sealed.py"
    try:
        timeout_seconds = int(env.get("SUITE_TIMEOUT_SECONDS", "3300"))
        argv_list = build_argv(sys.executable, wrapper, suite, log_dir, recipient, command, env.get("PATH", ""),
                               timeout_seconds=timeout_seconds)
    except ValueError:
        print("run_suite: invalid suite deadline", flush=True)
        return 2
    return run(argv_list, env=env, check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
