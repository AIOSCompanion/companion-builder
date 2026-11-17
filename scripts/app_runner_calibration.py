"""Calibrate the new app's release checks on the hosted runners, with public synthetic data only.

The release workflow's native checks depend on the runner, not only on the app. This proves each instrument
there before a real release relies on it, with a known-good control and a negative control:

Windows (windows-2022): the packaged-window check needs the WebView2 runtime the Wails window uses, an
interactive desktop, and its window finder (app_release_windows.window_for). Notepad's window must be found;
a process without a window must not be.

macOS (macos-15): the real unsigned packaging path makes a ZIP that unpacks to exactly Companion.app, and the
signed-archive verifier refuses that unsigned app under the signed name. Gatekeeper's state and the presence
of Apple's Developer ID intermediates on the image are reported, since the signing path relies on neither.

Prints facts and verdicts only. No secret, private checkout or release is involved.
"""
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))

WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def webview2_version():
    import winreg
    for hive, prefix in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\"), (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\"),
                         (winreg.HKEY_CURRENT_USER, "Software\\")):
        try:
            with winreg.OpenKey(hive, prefix + WEBVIEW2_CLIENT) as key:
                value, _ = winreg.QueryValueEx(key, "pv")
                if value and value != "0.0.0.0":
                    return value
        except OSError:
            continue
    return None


def windows():
    from app_release_windows import window_for
    version = webview2_version()
    if not version:
        raise RuntimeError("the WebView2 runtime is absent: the packaged window cannot open on this runner")
    print(f"WebView2 runtime {version} is installed")
    # The negative control has no window at all, not even a console window.
    quiet = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    notepad = subprocess.Popen(["notepad.exe"])
    try:
        deadline, handle = time.monotonic() + 30, None
        while handle is None and time.monotonic() < deadline:
            if notepad.poll() is not None:
                raise RuntimeError("the known-good window control exited")
            handle = window_for(notepad.pid)
            time.sleep(0.2)
        if handle is None:
            raise RuntimeError("no visible window was found for the known-good control: no interactive desktop")
        if window_for(quiet.pid) is not None:
            raise RuntimeError("a process without a window was reported as having one")
        print("Window finder calibrated: a real window is found, a windowless process is not")
    finally:
        for process in (notepad, quiet):
            process.kill()
            process.wait(timeout=10)


def macos():
    import app_release_macos
    status = subprocess.run(["spctl", "--status"], capture_output=True, text=True, check=False)
    print(f"Gatekeeper: {(status.stdout + status.stderr).strip() or 'unknown'}")
    for name in ("Developer ID Certification Authority",):
        found = subprocess.run(["security", "find-certificate", "-a", "-c", name, "/Library/Keychains/System.keychain"],
                               capture_output=True, text=True, check=False)
        print(f"System keychain copies of '{name}': {found.stdout.count('keychain:')} (the signer imports its own)")
    with tempfile.TemporaryDirectory(prefix="calibration-", dir=os.environ.get("RUNNER_TEMP") or None) as temporary:
        root = Path(temporary)
        bundle = root / "Companion_0.0.1-calibration.1_darwin_arm64.app"
        (bundle / "Contents/MacOS").mkdir(parents=True)
        executable = bundle / "Contents/MacOS/Companion"
        executable.write_text("#!/bin/sh\necho 0.0.1-calibration.1\n")
        executable.chmod(0o755)
        (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "Companion", "CFBundleShortVersionString": "0.0.1", "CFBundlePackageType": "APPL"}))
        app_release_macos.package(root, "v0.0.1-calibration.1", {}, unsigned=True)
        archive = root / "Companion_0.0.1-calibration.1_darwin_arm64_unsigned-test.zip"
        extracted = root / "extracted"
        extracted.mkdir()
        subprocess.run(["ditto", "-x", "-k", str(archive), str(extracted)], check=True)
        entries = sorted(path.name for path in extracted.iterdir())
        if entries != ["Companion.app"]:
            raise RuntimeError(f"the unsigned test ZIP unpacks to {entries}, not exactly Companion.app")
        print("Unsigned test ZIP unpacks to exactly Companion.app")
        shutil.copyfile(archive, root / "Companion_0.0.1-calibration.1_darwin_arm64.zip")
        try:
            app_release_macos.verify_signed_archive(root, "v0.0.1-calibration.1")
        except Exception as error:
            print(f"Signed-archive verifier refused the unsigned app as it must ({type(error).__name__})")
        else:
            raise RuntimeError("the signed-archive verifier accepted an unsigned app")


def main():
    try:
        if sys.platform == "win32":
            windows()
        elif sys.platform == "darwin":
            macos()
        else:
            raise RuntimeError("calibration runs on the Windows and macOS release runners")
        print("App release runner calibration passed")
        return 0
    except Exception as error:
        print(f"App release runner calibration failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
