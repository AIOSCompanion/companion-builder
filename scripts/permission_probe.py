#!/usr/bin/env python3
"""Prove the untrusted test user cannot touch the things the trusted steps are gated on, by errno, not by text.

The probe is one Python script. It runs once as the test user, through the same sudo path the install and the
suite use, and once as the runner (the control). For each operation it prints the errno it got. The verdicts:

  EACCES or EPERM  -> the operation was refused: the required outcome for an attack
  OK               -> the operation succeeded: a breach
  anything else    -> not a permission denial, so the probe refuses to certify (ENOENT proves nothing)

Identity is confirmed on the first line (user=pr-tests). The positive control is an exclusive create under the
PR checkout as the test user: it must be OK, or a denial elsewhere proves nothing about the sudo leg.

Gates (each must be refused, or the probe fails):
  writes  GITHUB_ENV, GITHUB_OUTPUT, GITHUB_PATH; the snapshot manifest; a new file in the snapshot; a rename of
          the snapshot; a new file in RUNNER_TEMP; a new directory RUNNER_TEMP/sealed
  PATH    a new file in every existing trusted PATH directory; in the nearest existing ancestor of every missing
          trusted PATH entry; in the parent of every trusted PATH directory (the workspace root is skipped)
  interp  a new file in the trusted interpreter's stdlib and site-packages (sysconfig paths)
  reads   the canary and the credential patterns in RUNNER_TEMP (git-credentials*, *.p12, *.p8, AuthKey_*,
          signing-env-*), so the checkout's credentials cannot be read

Reported, not gated: the runner install directory's .runner, .credentials and .credentials_rsaparams, and the
worker process's environment. The runner root comes from the Runner.Listener process's executable (its
grandparent directory); the worker is the Runner.Worker process. Each is reported as absent, denied, or READABLE.
READABLE prints a warning. The runner's home listing is reported too. Those layouts are only known on the
hosted image, so the first run decides whether they become gates.

The user's own ~/.local/bin is exempt from the PATH checks: no trusted step's PATH includes it.

    python3 -I -B scripts/permission_probe.py --trusted "$RUNNER_TEMP/trusted" --workdir pr --workspace "$GITHUB_WORKSPACE" --path "$PATH" --home /home/runner
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

CREDENTIAL_PATTERNS = ("git-credentials*", "*.p12", "*.p8", "AuthKey_*", "signing-env-*")
RUNNER_REPORT_FILES = (".runner", ".credentials", ".credentials_rsaparams")
CONTROL_NAME = "permission-probe-control"
REFUSED = frozenset({"EACCES", "EPERM"})

# Runs as the test user (and as the runner for the controls). Prints the identity, then label<TAB>errno per operation.
PROBE_SCRIPT = r'''
import errno, getpass, json, os, sys
ops = json.loads(sys.argv[1])
print("user=" + getpass.getuser())
for label, kind, a, b in ops:
    try:
        if kind == "create":
            fd = os.open(a, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            os.remove(a)
        elif kind == "append":
            fd = os.open(a, os.O_WRONLY | os.O_APPEND)
            os.close(fd)
        elif kind == "mkdir":
            os.mkdir(a)
            os.rmdir(a)
        elif kind == "rename":
            os.rename(a, b)
            os.rename(b, a)
        elif kind == "read":
            with open(a, "rb") as stream:
                stream.read(1)
        elif kind == "listdir":
            os.listdir(a)
        result = "OK"
    except OSError as error:
        result = errno.errorcode.get(error.errno, "E%d" % (error.errno or 0))
    print(label + "\t" + result)
'''


class ProbeError(Exception):
    pass


def _text(value):
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def run_ops(ops, *, interpreter, user=None, run=subprocess.run):
    """Run the operations as `user` (or as the runner when user is None); return ({label: errno_or_OK}, identity)."""
    prefix = ["sudo", "-n", "-u", user, "--"] if user else []
    argv = [*prefix, "env", "-i", "PATH=/usr/bin:/bin", interpreter, "-I", "-B", "-c", PROBE_SCRIPT, json.dumps(ops)]
    result = run(argv, capture_output=True, text=True, check=False)
    lines = [line for line in _text(result.stdout).splitlines() if line]
    if not lines or not lines[0].startswith("user="):
        raise ProbeError("the probe did not run and print its identity (sudo or the interpreter failed)")
    identity = lines[0][len("user="):]
    results = {}
    for line in lines[1:]:
        label, _, code = line.rpartition("\t")
        results[label] = code
    return results, identity


def _process_name(pid_dir, readlink):
    try:
        return Path(readlink(str(pid_dir / "exe"))).name
    except OSError:
        return None


def discover_runner_root(proc="/proc", readlink=os.readlink):
    """The runner install root: the grandparent of Runner.Listener's executable (bin/Runner.Listener -> the root).

    Returns None when no Runner.Listener process is visible; the report then says so, and nothing is gated on it.
    """
    for entry in sorted(Path(proc).iterdir()):
        if entry.name.isdigit() and _process_name(entry, readlink) == "Runner.Listener":
            return Path(readlink(str(entry / "exe"))).parent.parent.as_posix()
    return None


def discover_worker_pid(proc="/proc", readlink=os.readlink):
    """The Runner.Worker process id, matched on the executable's basename; None when it is not visible."""
    for entry in sorted(Path(proc).iterdir()):
        if entry.name.isdigit() and _process_name(entry, readlink) == "Runner.Worker":
            return int(entry.name)
    return None


def _nearest_existing(path):
    current = Path(path)
    while not current.exists():
        if current.parent == current:
            return None
        current = current.parent
    return current


def build_attacks(*, trusted, runner_temp, workspace, path_dirs, github_env, github_output, github_path,
                  interpreter_paths, user_home):
    """(label, kind, a, b) for every gated operation the test user must be refused."""
    trusted, runner_temp = Path(trusted), Path(runner_temp)
    attacks = [
        ("write GITHUB_ENV", "append", github_env, None),
        ("write GITHUB_OUTPUT", "append", github_output, None),
        ("write GITHUB_PATH", "append", github_path, None),
        ("write the snapshot manifest", "append", str(trusted / "MANIFEST.sha256"), None),
        ("create a file in the snapshot", "create", str(trusted / "probe-new"), None),
        ("rename the snapshot", "rename", str(trusted), str(trusted) + ".renamed"),
        ("create a file in RUNNER_TEMP", "create", str(runner_temp / "probe-new"), None),
        ("create RUNNER_TEMP/sealed", "mkdir", str(runner_temp / "sealed"), None),
    ]
    for directory in path_dirs:
        directory = Path(directory)
        if not str(directory) or str(directory).startswith(user_home):
            continue  # the user's own bin: writable by design, on no trusted step's PATH
        if directory.is_dir():
            attacks.append((f"create a file in trusted PATH directory {directory}", "create",
                            str(directory / "probe-new"), None))
            parent = directory.parent
            if parent != directory and str(parent) != str(workspace) and parent.exists():
                attacks.append((f"create a file in the parent of trusted PATH directory {directory}", "create",
                                str(parent / "probe-new"), None))
        else:
            ancestor = _nearest_existing(directory)
            if ancestor is not None:
                attacks.append((f"create a file in the nearest ancestor of missing PATH entry {directory} ({ancestor})",
                                "create", str(ancestor / "probe-new"), None))
    for interpreter_path in interpreter_paths:
        if not Path(interpreter_path).is_dir():
            continue  # a directory that does not exist cannot be attacked; ENOENT would prove nothing
        attacks.append((f"create a file in the trusted interpreter path {interpreter_path}", "create",
                        str(Path(interpreter_path) / "probe-new"), None))
    attacks.append(("read the canary", "read", str(runner_temp / "permission-canary"), None))
    for pattern in CREDENTIAL_PATTERNS:
        for match in sorted(Path(runner_temp).glob(pattern)):
            attacks.append((f"read credential file {match.name}", "read", str(match), None))
    return attacks


def require_targets_exist(attacks, home):
    """Every gated target must exist as the runner before the test user is asked to touch it.

    A wrong path fails as ENOENT, which the errno rule refuses, but only after the fact. Checking here, as the
    runner, makes a wrong target stop the probe with the path named. A new file needs its parent to exist.
    """
    for label, kind, target, _second in attacks:
        if kind in ("create", "mkdir"):
            parent = Path(target).parent
            if not parent.is_dir():
                raise ProbeError(f"'{label}': the parent {parent} does not exist as the runner; a wrong path would fail "
                                 "for the wrong reason, so nothing is certified")
        elif not Path(target).exists():
            raise ProbeError(f"'{label}': the target {target} does not exist as the runner; a wrong path would fail "
                             "for the wrong reason, so nothing is certified")
    if not Path(home).is_dir():
        raise ProbeError(f"the home directory {home} does not exist as the runner; nothing is certified")


def report_runner_files(*, runner_root, worker_pid, proc, user, interpreter, run):
    """Report, do not gate: the runner's install-dir files and the worker's environ, each absent, denied or READABLE.

    The runner's own read of each existing file must succeed first, or the report would mean nothing.
    """
    lines = []
    if runner_root is None:
        lines.append("WARNING: no Runner.Listener process is visible; the runner install directory was not reported")
    else:
        for name in RUNNER_REPORT_FILES:
            label = f"runner file {name}"
            path = f"{runner_root}/{name}"
            lines.append(_report_one(label, path, user=user, interpreter=interpreter, run=run))
    if worker_pid is None:
        lines.append("WARNING: no Runner.Worker process is visible; the worker environment was not reported")
    else:
        path = f"{proc}/{worker_pid}/environ"
        lines.append(_report_one(f"worker environment {path}", path, user=user, interpreter=interpreter, run=run))
    return lines


def _report_one(label, path, *, user, interpreter, run):
    if not Path(path).exists():
        return f"{label}: absent"
    control, _ = run_ops([["runner: read " + label, "read", path, None]], interpreter=interpreter, run=run)
    if control.get("runner: read " + label) != "OK":
        raise ProbeError(f"control failed: the runner cannot read its own {label}; its report would mean nothing")
    results, _ = run_ops([[label, "read", path, None]], interpreter=interpreter, user=user, run=run)
    code = results.get(label)
    if code in REFUSED:
        return f"{label}: denied ({code})"
    if code == "OK":
        return f"WARNING: {label}: READABLE by the test user; decide whether this becomes a gate"
    raise ProbeError(f"'{label}' reported {code}, which is not a permission result; nothing is certified")


def probe(*, user="pr-tests", github_env, github_output, github_path, trusted, runner_temp, workdir, workspace,
          path_dirs, home, interpreter, runner_root=None, worker_pid=None, interpreter_paths=(), proc="/proc",
          run=subprocess.run):
    for label, value in (("GITHUB_ENV", github_env), ("GITHUB_OUTPUT", github_output), ("GITHUB_PATH", github_path),
                         ("the trusted snapshot", trusted), ("RUNNER_TEMP", runner_temp), ("the PR checkout", workdir)):
        if not value:
            raise ProbeError(f"{label} must be given")
    user_home = f"/home/{user}"
    runner_temp_path = Path(runner_temp)

    # Control 1 (runner): the same kinds of operation on the runner's own files must be OK.
    canary = runner_temp_path / "permission-canary"
    descriptor = os.open(canary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(descriptor, b"canary\n")
    os.close(descriptor)
    try:
        control_ops = [
            ["runner: append GITHUB_ENV", "append", github_env, None],
            ["runner: read canary", "read", str(canary), None],
            ["runner: create in RUNNER_TEMP", "create", str(runner_temp_path / "control-write"), None],
        ]
        runner_results, _ = run_ops(control_ops, interpreter=interpreter, run=run)
        for label, _kind, *_ in control_ops:
            if runner_results.get(label) != "OK":
                raise ProbeError(f"control failed: the runner's own {label.split(': ', 1)[1]} returned "
                                 f"{runner_results.get(label)}; a denial would prove nothing")

        # Control 2 (the sudo leg): as the test user, a create under its own checkout must be OK. The identity
        # line also shows the interpreter the trusted steps use is executable by the test user.
        positive = str(Path(workdir) / CONTROL_NAME)
        results, identity = run_ops([["positive: create under the checkout", "create", positive, None]],
                                    interpreter=interpreter, user=user, run=run)
        if identity != user:
            raise ProbeError(f"the probe did not run as the test user (identity was {identity!r})")
        if results.get("positive: create under the checkout") != "OK":
            raise ProbeError("positive control failed: the test user cannot write its own checkout through sudo, "
                             "so a denial proves nothing")
        run(["sudo", "-n", "rm", "-f", positive], check=False)
        lines = [f"interpreter {interpreter} ran as {identity}"]

        attacks = build_attacks(trusted=trusted, runner_temp=runner_temp, workspace=workspace, path_dirs=path_dirs,
                                github_env=github_env, github_output=github_output, github_path=github_path,
                                interpreter_paths=interpreter_paths, user_home=user_home)
        require_targets_exist(attacks, home)
        ops = [[label, kind, a, b] for label, kind, a, b in attacks]
        results, identity = run_ops(ops, interpreter=interpreter, user=user, run=run)
        if identity != user:
            raise ProbeError(f"the probe did not run as the test user (identity was {identity!r})")
        for label, _, _, _ in attacks:
            code = results.get(label)
            if code is None:
                raise ProbeError(f"no result for '{label}'; nothing is certified")
            if code == "OK":
                raise ProbeError(f"the test user could {label}; the trusted steps are not protected")
            if code not in REFUSED:
                raise ProbeError(f"'{label}' failed with {code}, which is not a permission denial; nothing is certified")
            lines.append(f"{label}: refused ({code})")

        lines += report_runner_files(runner_root=runner_root, worker_pid=worker_pid, proc=proc, user=user,
                                     interpreter=interpreter, run=run)

        listing = run_ops([["list home", "listdir", home, None]], interpreter=interpreter, user=user, run=run)[0]
        code = listing.get("list home")
        if code in REFUSED:
            lines.append(f"home listing ({home}): refused ({code})")
        elif code == "OK":
            lines.append(f"home listing ({home}): readable; the 711 lock did not apply on this image")
        else:
            raise ProbeError(f"the home listing failed with {code}; nothing is certified")
        return lines
    finally:
        canary.unlink(missing_ok=True)
        (runner_temp_path / "control-write").unlink(missing_ok=True)


def build_parser():
    """The probe's command line. test.yml's call is parsed against this in the tests, so a missing argument fails here."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", default="pr-tests")
    parser.add_argument("--trusted", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--path", default=None)
    parser.add_argument("--home", default="/home/runner")
    parser.add_argument("--require-runner-denials", action="store_true",
                        help="fail if runner files/environment are readable or discovery is unavailable")
    return parser


def main(argv=None, *, run=subprocess.run, env=None, proc="/proc", readlink=os.readlink):
    args = build_parser().parse_args(argv)
    env = os.environ if env is None else env
    search = [d for d in (args.path if args.path is not None else env.get("PATH", "")).split(os.pathsep) if d]
    interpreter = sys.executable
    interpreter_paths = sorted({sysconfig.get_paths()[key] for key in ("stdlib", "platstdlib", "purelib", "platlib")})
    try:
        lines = probe(user=args.user, github_env=env.get("GITHUB_ENV"), github_output=env.get("GITHUB_OUTPUT"),
                      github_path=env.get("GITHUB_PATH"), trusted=args.trusted, runner_temp=env.get("RUNNER_TEMP"),
                      workdir=args.workdir, workspace=args.workspace or env.get("GITHUB_WORKSPACE", ""),
                      path_dirs=search, home=args.home,
                      runner_root=discover_runner_root(proc, readlink), worker_pid=discover_worker_pid(proc, readlink),
                      interpreter=interpreter, interpreter_paths=interpreter_paths, proc=proc, run=run)
    except ProbeError as error:
        print(f"::error::permission probe: {error}")
        return 1
    for line in lines:
        print(f"permission probe: {line}")
    if args.require_runner_denials and any("READABLE" in line or "no Runner." in line for line in lines):
        print("::error::permission probe: runner configuration access is not denied; private-source work is blocked")
        return 1
    runner_items = sum(1 for line in lines if line.startswith(("runner file ", "worker environment ")))
    warnings = sum(1 for line in lines if line.startswith("WARNING") or "READABLE" in line or "readable; the 711" in line)
    print(f"permission probe: calibrated; the test user is refused every gated write and read the trusted steps "
          f"depend on; {runner_items} runner item(s) reported, {warnings} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
