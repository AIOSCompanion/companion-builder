"""Platform-specific protection for the new app's trusted release-script snapshot."""
import csv
import io
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_trusted


def checked(args):
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        tool = args[0] if args[0] in ("icacls", "whoami", "takeown", "sudo") else "platform tool"
        raise RuntimeError(f"Trusted snapshot protection failed ({tool}, exit {result.returncode}); platform output withheld")
    return result.stdout


def windows_sid():
    rows = list(csv.reader(io.StringIO(checked(["whoami", "/user", "/fo", "csv", "/nh"]))))
    if len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"S-1-[0-9-]+", rows[0][1]):
        raise RuntimeError("Runner identity could not be established")
    return rows[0][1]


def protect_windows(destination, sid):
    # SYSTEM owns the snapshot; the runner can read/execute but cannot write,
    # delete or add files. Explicit administrator grants are intentionally absent.
    # Tagged release code is trusted; an elevated adversary could still take
    # ownership, just as a Linux release dependency could invoke sudo.
    from app_release_windows_acl import set_owners, set_permissions
    set_owners(destination, "S-1-5-18")
    # Inheritance flags are meaningful only for directories. Applying the same
    # inheritable ACE to a file can leave it with no effective read grant.
    set_permissions(destination, sid, readonly=True)


def verify_write_denied(destination):
    destination = Path(destination)
    # Probe actual access rather than trusting command success or mode bits.
    try:
        with (destination / snapshot_trusted.MANIFEST).open("r+b"):
            pass
    except PermissionError:
        pass
    else:
        raise RuntimeError("Trusted snapshot is writable by the build user")
    probe = destination / ".builder-write-control"
    try:
        with probe.open("xb"):
            pass
    except PermissionError:
        pass
    else:
        probe.unlink()
        raise RuntimeError("Trusted snapshot directory permits adding files")


def create(source, destination):
    if os.name == "nt":
        count = snapshot_trusted.snapshot(source, destination)
        protect_windows(destination, windows_sid())
    else:
        # Numeric uid/gid 0 work on both Linux(root) and macOS(wheel).
        def protect(command):
            command = ["0:0" if value == "root:root" else value for value in command]
            checked(command)
        count = snapshot_trusted.snapshot(source, destination, sudo=protect)
    verify_write_denied(destination)
    snapshot_trusted.verify(destination)
    return count


def main():
    try:
        if len(sys.argv) != 2:
            raise ValueError("Expected snapshot destination")
        count = create(Path(__file__).resolve().parent, Path(sys.argv[1]))
        print(f"New-app release snapshot protected and verified: {count} files")
        return 0
    except Exception:
        print("New-app release snapshot failed; no private code was run", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
