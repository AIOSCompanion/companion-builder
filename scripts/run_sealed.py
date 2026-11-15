#!/usr/bin/env python3
"""Run one step with its output sealed to the team log key.

Every step of the public builder that runs code goes through this wrapper: the release rows, the
signing and publish steps, the install check and the PR tests. The step's stdout and stderr go into an
encryption pipe. They are never written to this process's stdout, and no plaintext of them is kept on disk.

What this prints is only: the step name, the exit code, the duration, and on failure the last error
class, from a fixed allowlist of standard Python exceptions ("other" for anything else). Never a path,
never an output line.

    python -B scripts/run_sealed.py --name "mac sidecars" --log-dir sealed/logs --recipient "$RECIPIENT" \
        --export-output sha -- python -B scripts/release/runtimes.py key

Options, all before the --:
  --name NAME          the step's name (sanitised for file names; printed as given)
  --log-dir DIR        where the sealed log goes
  --recipient KEY      the team log key (default: $LOG_RECIPIENT)
  --export-env NAME    the child may set this GITHUB_ENV name; only the exact names given come back (repeatable)
  --export-output NAME the child may set this GITHUB_OUTPUT key; only the exact keys given come back (repeatable)
  --junit PATH         after the command, print the counts line from the junit report (counts only), then seal it
  --seal-dir DIR       after the command, seal every file in DIR (repeatable)
  --seal-file PATH     after the command, seal one file (repeatable)

Side channels are closed by default. The child never receives GITHUB_STEP_SUMMARY, GITHUB_STATE,
GITHUB_PATH, or any ACTIONS_* variable, so it cannot publish a summary, upload, or cache. It receives
GITHUB_ENV or GITHUB_OUTPUT only when a step names the exact keys it expects to set, and then only those
lines are copied back. Untrusted code (the PR tests) gets neither.

Refusals happen before the command runs: no recipient, no gpg, or no command. So unsealed output is
never produced. Sealing removes each plaintext only after its ciphertext exists.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import summarize_junit  # noqa: E402  (same folder; prints nothing on import)

ERROR_CLASS = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Failure))\b")
# Only these names are printed. A private class (for example FooBarError) prints as "other".
STANDARD_ERRORS = frozenset({
    "AssertionError", "AttributeError", "BrokenPipeError", "ConnectionError", "EOFError",
    "FileExistsError", "FileNotFoundError", "ImportError", "IndexError", "IsADirectoryError",
    "KeyError", "LookupError", "ModuleNotFoundError", "NameError", "NotADirectoryError",
    "NotImplementedError", "OSError", "OverflowError", "PermissionError", "RecursionError",
    "RuntimeError", "StopIteration", "SyntaxError", "SystemError", "TimeoutError", "TypeError",
    "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError", "ValueError", "ZeroDivisionError",
    "CalledProcessError", "JSONDecodeError", "BlockingIOError", "ChildProcessError",
    "InterruptedError", "ProcessLookupError", "ReferenceError", "UnboundLocalError",
})
TAIL_BYTES = 256 * 1024
CHUNK = 64 * 1024

# The child never gets these. GITHUB_ENV and GITHUB_OUTPUT come back only through the explicit allowlists.
REMOVED_NAMES = ("GITHUB_STEP_SUMMARY", "GITHUB_STATE", "GITHUB_PATH", "GITHUB_ENV", "GITHUB_OUTPUT")
REMOVED_PREFIXES = ("ACTIONS_",)


class RefusedError(Exception):
    """Raised before the command runs. The message names the cause and nothing from the command."""


def safe_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not slug:
        raise RefusedError("A step needs a name.")
    return slug


def encrypt_stream_pipe(gpg, recipient, target):
    """Start gpg reading plaintext from stdin and writing ciphertext to target."""
    target.parent.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([*gpg, "--batch", "--yes", "--trust-model", "always", "--encrypt",
                             "-r", recipient, "-o", str(target)],
                            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def seal_path(gpg, recipient, source, target):
    """Encrypt one file to target, then remove the plaintext only if the ciphertext exists."""
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([*gpg, "--batch", "--yes", "--trust-model", "always", "--encrypt",
                             "-r", recipient, "-o", str(target), str(source)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        return False
    os.unlink(source)
    return True


def last_error_class(tail):
    """The last error-looking name in the output, if it is a standard one; "other" for anything else."""
    text = tail.decode("utf-8", errors="replace").splitlines()[-200:]
    found = ERROR_CLASS.findall("\n".join(text))
    if not found:
        return None
    return found[-1] if found[-1] in STANDARD_ERRORS else "other"


def child_environment(base, scratch, export_env=(), export_output=()):
    """Return (child env, copy-back specs). GITHUB_ENV and GITHUB_OUTPUT exist only when names were allowlisted."""
    env = {key: value for key, value in base.items()
           if key not in REMOVED_NAMES and not any(key.startswith(prefix) for prefix in REMOVED_PREFIXES)}
    copies = []
    for real_key, allowed in (("GITHUB_ENV", export_env), ("GITHUB_OUTPUT", export_output)):
        real = base.get(real_key)
        if allowed and real:
            scratch_file = Path(scratch) / real_key.lower()
            scratch_file.touch()
            env[real_key] = str(scratch_file)
            copies.append((Path(real), scratch_file, frozenset(allowed)))
    return env, copies


def copy_back(copies):
    """Append to the real files only the lines whose name is on that step's allowlist. Returns (copied, refused)."""
    copied = refused = 0
    for real, scratch_file, allowed in copies:
        if not scratch_file.exists():
            continue
        keep = []
        for line in scratch_file.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True):
            name = line.split("=", 1)[0] if "=" in line else ""
            if name in allowed:
                keep.append(line)
                copied += 1
            else:
                refused += 1
        if keep:
            with open(real, "a", encoding="utf-8", newline="\n") as stream:
                stream.writelines(keep)
    return copied, refused


def untrusted_argv(command, user, path):
    """Run a command as an unprivileged user with a clean environment.

    The PR's install and tests are untrusted code. Running them as a separate user means they cannot write
    the trusted scripts in the workspace, cannot read this process's environment through /proc, and cannot
    write the runner's GITHUB_ENV, GITHUB_OUTPUT or GITHUB_PATH files. Sudo is used only to switch user.
    """
    return ["sudo", "-n", "-u", user, "--", "env", "-i",
            f"PATH=/home/{user}/.local/bin:{path}", f"HOME=/home/{user}", "LANG=C.UTF-8",
            "PYTHONDONTWRITEBYTECODE=1", *command]


def run_step(name, command, *, log_dir, recipient, gpg=("gpg",), junit=None, seal_dirs=(), seal_files=(),
             export_env=(), export_output=(), as_user=None, popen=subprocess.Popen, clock=time.monotonic, out=None,
             base_env=None):
    """Run one step. The child gets a sanitised environment; only allowlisted file outputs come back."""
    base = dict(os.environ if base_env is None else base_env)
    scratch = tempfile.mkdtemp(prefix="run-sealed-")
    copies = []
    try:
        env, copies = child_environment(base, scratch, export_env=export_env, export_output=export_output)
        if as_user:
            command = untrusted_argv(command, as_user, env.get("PATH", ""))
        return _run_body(name, command, log_dir=log_dir, recipient=recipient, gpg=gpg, junit=junit,
                         seal_dirs=seal_dirs, seal_files=seal_files, popen=popen, clock=clock, out=out, env=env,
                         copies=copies)
    finally:
        copy_back(copies)
        shutil.rmtree(scratch, ignore_errors=True)


def _run_body(name, command, *, log_dir, recipient, gpg, junit, seal_dirs, seal_files, popen, clock, out, env,
              copies):
    out = sys.stdout if out is None else out
    if not recipient:
        raise RefusedError("No recipient for the log key; the step was not run.")
    if not command:
        raise RefusedError("No command given; the step was not run.")
    slug = safe_slug(name)
    log_dir = Path(log_dir)
    target = log_dir / f"{slug}.log.gpg"
    try:
        sealer = encrypt_stream_pipe(list(gpg), recipient, target)
    except OSError:
        raise RefusedError("gpg could not start; the step was not run.")
    started = clock()
    tail = bytearray()
    try:
        child = popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError:
        sealer.stdin.close()
        sealer.wait()
        print(f"step {name}: could not start (exit 127)", file=out, flush=True)
        return 127
    seal_broken = False
    while True:
        chunk = child.stdout.read(CHUNK)
        if not chunk:
            break
        tail += chunk
        del tail[:-TAIL_BYTES]
        if seal_broken:
            continue  # keep draining so the child never blocks on a full pipe
        try:
            sealer.stdin.write(chunk)
        except OSError:
            seal_broken = True
    code = child.wait()
    try:
        sealer.stdin.close()
    except OSError:
        seal_broken = True
    if sealer.wait() != 0 or seal_broken or not target.is_file() or target.stat().st_size == 0:
        print(f"step {name}: output could not be sealed; treat this step as failed", file=out, flush=True)
        return 1

    elapsed = int(clock() - started)
    print(f"step {name}: exit {code} in {elapsed}s", file=out, flush=True)
    if code != 0:
        error_class = last_error_class(bytes(tail))
        print(f"last error class: {error_class or 'none recognised'}", file=out, flush=True)

    if junit is not None and Path(junit).is_symlink():
        # The junit path is written by untrusted code. A symlink there could point at a runner file, and the
        # wrapper would seal it. A symlink is refused, not followed.
        print(f"step {name}: the junit report is a symbolic link; it was not sealed", file=out, flush=True)
        return 1
    if junit is not None and Path(junit).is_file():
        try:
            total, failed, errored, skipped = summarize_junit.summarise(Path(junit))
        except Exception:  # a malformed report is reported as such; its text is never printed
            print(f"junit report unreadable; suite exit {code}", file=out, flush=True)
        else:
            passed = total - failed - errored - skipped
            print(f"tests {total}, passed {passed}, failed {failed}, errors {errored}, skipped {skipped}; "
                  f"suite exit {code}", file=out, flush=True)
        # The junit file was written by untrusted code, so it is never removed here. The wrapper seals a
        # staged copy; the workflow removes the original.
        staged = log_dir / f".{slug}-junit.staged"
        shutil.copyfile(junit, staged)
        if not seal_path(list(gpg), recipient, staged, log_dir / f"{slug}-junit.xml.gpg"):
            print(f"step {name}: the junit report could not be sealed", file=out, flush=True)
            return 1

    for directory in seal_dirs:
        directory = Path(directory)
        if not directory.is_dir():
            continue
        for path in sorted(p for p in directory.rglob("*") if p.is_file() and not p.is_symlink()):
            rel = path.relative_to(directory)
            target = log_dir / f"{slug}-{directory.name}" / rel.parent / (rel.name + ".gpg")
            if not seal_path(list(gpg), recipient, path, target):
                print(f"step {name}: a file in {directory.name} could not be sealed", file=out, flush=True)
                return 1

    for path in seal_files:
        path = Path(path)
        if path.is_file() and not seal_path(list(gpg), recipient, path, log_dir / f"{slug}-{path.name}.gpg"):
            print(f"step {name}: a file could not be sealed", file=out, flush=True)
            return 1

    if copies:
        copied, refused = copy_back(copies)
        copies.clear()
        if refused:
            print(f"step {name}: {refused} environment or output line(s) not on the allowlist were not copied",
                  file=out, flush=True)
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        print("run_sealed: give the command after --", file=sys.stderr)
        return 2
    split = argv.index("--")
    options, command = argv[:split], argv[split + 1:]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", required=True)
    parser.add_argument("--log-dir", required=True)
    parser.add_argument("--recipient", default=os.environ.get("LOG_RECIPIENT", ""))
    parser.add_argument("--gpg", default="gpg")
    parser.add_argument("--export-env", action="append", default=[])
    parser.add_argument("--export-output", action="append", default=[])
    parser.add_argument("--as-user", default=None, help="run the command as this unprivileged user")
    parser.add_argument("--junit", default=None)
    parser.add_argument("--seal-dir", action="append", default=[])
    parser.add_argument("--seal-file", action="append", default=[])
    args = parser.parse_args(options)
    try:
        return run_step(args.name, command, log_dir=args.log_dir, recipient=args.recipient, gpg=(args.gpg,),
                        junit=args.junit, seal_dirs=args.seal_dir, seal_files=args.seal_file,
                        export_env=args.export_env, export_output=args.export_output, as_user=args.as_user)
    except RefusedError as error:
        print(f"run_sealed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
