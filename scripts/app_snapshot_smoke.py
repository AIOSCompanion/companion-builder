"""Calibrate real release snapshot protection on public synthetic files only."""
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_release_snapshot import checked, create, verify_write_denied, windows_sid
from snapshot_trusted import SnapshotError, verify


def release_for_cleanup(destination):
    if os.name == "nt":
        # This owns only a disposable snapshot beneath this command's temporary
        # directory, never the repository or any production/user directory.
        sid = windows_sid()
        from app_release_windows_acl import set_owners, set_permissions
        set_owners(destination, sid)
        set_permissions(destination, sid, readonly=False)
    else:
        checked(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(destination)])
    for path in [destination, *destination.rglob("*")]:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if path.is_dir() else 0))


def main():
    parent = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()).resolve()
    temporary = Path(tempfile.mkdtemp(prefix="snapshot-smoke-", dir=parent)).resolve()
    if temporary.parent != parent or not temporary.name.startswith("snapshot-smoke-"):
        raise RuntimeError("Disposable snapshot path is outside its owned temporary directory")
    destination = temporary / "trusted"
    try:
        source = temporary / "source"
        source.mkdir()
        (source / "public_control.py").write_text("print('synthetic-control')\n")
        # Known writable control: the instrument must reject it.
        (source / "MANIFEST.sha256").write_text("writable control")
        try:
            verify_write_denied(source)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Writable control was not detected")
        (source / "MANIFEST.sha256").unlink()
        create(source, destination)
        if verify(destination) != 1:
            raise RuntimeError("Snapshot contents were not recorded")
        release_for_cleanup(destination)
        (destination / "public_control.py").write_text("print('mutated-control')\n")
        try:
            verify(destination)
        except SnapshotError:
            pass
        else:
            raise RuntimeError("Changed snapshot was not detected")
        print("Release snapshot controls passed: writes denied, unchanged hashes accepted, mutation rejected")
        return 0
    finally:
        if destination.exists():
            release_for_cleanup(destination)
        shutil.rmtree(temporary)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        detail = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        print("Release snapshot smoke failed: " + detail + "; no private data was used", file=sys.stderr)
        sys.exit(1)
