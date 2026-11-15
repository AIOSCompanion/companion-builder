#!/usr/bin/env python3
"""Prove the test namespace is isolated: only loopback exists, and the probe's connection is refused.

Calibration first: from the runner, as the untrusted test user, github.com must answer. Otherwise a blocked
result proves nothing. Then the claim, inside `unshare --net`, as the same user:
  1. the probe runs (a sentinel is printed);
  2. the only network interface is lo;
  3. curl exits with exactly 6 (could not resolve) or 7 (could not connect).
Any other exit, including 0, 28 (timeout), 127 (curl missing) or a failed unshare, fails the probe.

    python3 -I -B scripts/egress_probe.py [--user pr-tests]

Exit 0 only when all three hold. The output is one line.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

PROBE_URL = "https://github.com"
ALLOWED_CURL_FAILURES = frozenset({6, 7})  # could not resolve host; could not connect


class ProbeError(Exception):
    pass


def _text(value):
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def probe(user="pr-tests", run=subprocess.run):
    outside = run(["sudo", "-n", "-u", user, "--", "curl", "-sS", "--max-time", "20", PROBE_URL, "-o", os.devnull],
                  capture_output=True, check=False)
    if outside.returncode != 0:
        raise ProbeError("calibration failed: the test user cannot reach github.com from this runner")
    script = (f"echo PROBE_RAN && ip -o link show && ip link set lo up && "
              f"{{ runuser -u {user} -- curl -sS --max-time 10 {PROBE_URL} -o /dev/null; echo CURL_RC=$?; }}")
    inside = run(["sudo", "-n", "unshare", "--net", "bash", "-c", script], capture_output=True, check=False)
    lines = _text(inside.stdout).splitlines()
    if "PROBE_RAN" not in lines:
        raise ProbeError("the probe did not run inside the namespace")
    links = [re.match(r"^\d+:\s*([^:@\s]+)", line).group(1) for line in lines if re.match(r"^\d+:\s*\S", line)]
    if links != ["lo"]:
        raise ProbeError("the namespace has an interface other than loopback")
    codes = [line for line in lines if line.startswith("CURL_RC=")]
    if not codes:
        raise ProbeError("curl did not run inside the namespace")
    try:
        code = int(codes[-1].split("=", 1)[1])
    except ValueError:
        raise ProbeError("the probe's exit code could not be read")
    if code == 0:
        raise ProbeError("an outbound request succeeded inside the no-network namespace")
    if code not in ALLOWED_CURL_FAILURES:
        raise ProbeError(f"curl exited {code} inside the namespace, not a connect or resolve failure; isolation is not proven")
    return "outbound probe: reachable outside the namespace, and inside it only loopback exists and the connection is refused"


def main(argv=None, *, run=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="pr-tests")
    args = parser.parse_args(argv)
    try:
        print(probe(user=args.user, run=run))
    except ProbeError as error:
        print(f"::error::egress probe: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
