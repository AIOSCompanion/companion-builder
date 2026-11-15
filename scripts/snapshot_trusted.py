#!/usr/bin/env python3
"""Snapshot the builder's trusted scripts outside the workspace, and verify them later.

Untrusted PR code runs as another user, but a hosted runner gives the job one workspace and one set of
environment files. The trusted scripts therefore never run from the workspace. Before any PR checkout or
install, this copies them to a directory outside the workspace, makes that directory root-owned and
read-only, and records a SHA-256 for every file. Each trusted step runs them from the snapshot with
`python -I -B`. Verification recomputes the hashes, so a changed file stops the job before the suite, and
before any sealing or upload.

    python -I -B scripts/snapshot_trusted.py snapshot --source scripts --dest "$RUNNER_TEMP/trusted"
    python -I -B "$RUNNER_TEMP/trusted/snapshot_trusted.py" verify --dest "$RUNNER_TEMP/trusted"

Prints only counts and verdicts, never a path from the content.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

MANIFEST = "MANIFEST.sha256"


class SnapshotError(Exception):
    pass


def _digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _files(root):
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name != MANIFEST and not p.is_symlink())


def snapshot(source, dest, *, sudo=None):
    source, dest = Path(source), Path(dest)
    if not source.is_dir():
        raise SnapshotError("the trusted scripts directory is missing")
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace and Path(dest).resolve().is_relative_to(Path(workspace).resolve()):
        raise SnapshotError("the snapshot must be outside the workspace")
    if dest.exists():
        raise SnapshotError("the snapshot directory already exists; refusing to reuse it")
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__"))
    lines = []
    for path in _files(dest):
        lines.append(f"{_digest(path)}  {path.relative_to(dest).as_posix()}\n")
    (dest / MANIFEST).write_text("".join(lines), encoding="utf-8", newline="\n")
    # Modes first, while the runner still owns the files; then ownership moves to root, which the runner cannot undo.
    for path in [dest, *dest.rglob("*")]:
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    if sudo is not None:
        sudo(["sudo", "-n", "chown", "-R", "root:root", str(dest)])
    return len(lines)


def verify(dest):
    dest = Path(dest)
    manifest = dest / MANIFEST
    if not manifest.is_file():
        raise SnapshotError("the snapshot manifest is missing")
    expected = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        expected[name] = digest
    actual = {p.relative_to(dest).as_posix(): _digest(p) for p in _files(dest)}
    actual.pop(MANIFEST, None)
    if set(expected) != set(actual):
        raise SnapshotError("the trusted scripts changed: a file was added or removed")
    changed = [name for name in expected if expected[name] != actual[name]]
    if changed:
        raise SnapshotError(f"the trusted scripts changed: {len(changed)} file(s) differ from the snapshot")
    return len(expected)


def main(argv=None, *, sudo=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    snap = sub.add_parser("snapshot")
    snap.add_argument("--source", default=str(Path(__file__).resolve().parent))
    snap.add_argument("--dest", required=True)
    check = sub.add_parser("verify")
    check.add_argument("--dest", required=True)
    args = parser.parse_args(argv)
    sudo = sudo if sudo is not None else (lambda cmd: subprocess.run(cmd, check=True))
    try:
        if args.command == "snapshot":
            count = snapshot(args.source, args.dest, sudo=sudo)
            print(f"snapshot: {count} trusted file(s) recorded")
        else:
            count = verify(args.dest)
            print(f"verify: {count} trusted file(s) match the snapshot")
    except (SnapshotError, subprocess.CalledProcessError) as error:
        print(f"snapshot_trusted: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
