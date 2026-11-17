"""Check the built Windows executables using an isolated fresh home and owned processes."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import tempfile
import time

from app_release_artifacts import filenames, version


def command(args, *, env, timeout=30):
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Windows package command failed; output withheld")
    return result.stdout.strip()


def window_for(pid):
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    found = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def inspect(handle, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        rect = wintypes.RECT()
        if owner.value == pid and user32.IsWindowVisible(handle) and user32.GetClientRect(handle, ctypes.byref(rect)):
            if rect.right - rect.left >= 200 and rect.bottom - rect.top >= 100:
                found.append(handle)
        return True
    user32.EnumWindows(callback_type(inspect), 0)
    if len(found) > 1:
        raise RuntimeError("Packaged test owns multiple unexpected visible windows")
    return found[0] if found else None


def check(root, tag, *, windows=None):
    if not (os.name == "nt" if windows is None else windows):
        raise ValueError("Windows package verification requires a Windows runner")
    desktop_name, cli_name = filenames(tag, "windows-amd64")
    root = Path(root).resolve()
    cli, desktop = root / cli_name, root / desktop_name
    # A fresh home selects no owner profile, model key, database or fixed port. WebView2 keeps its data folder in
    # here, and its helper processes can hold files briefly after the window has gone: removing the folder on this
    # ephemeral runner is not part of the verdict, so a locked file there must not fail checks that passed.
    with tempfile.TemporaryDirectory(prefix="companion-package-", ignore_cleanup_errors=True) as temporary:
        home = Path(temporary) / "home"
        env = {key: value for key, value in os.environ.items() if not any(
            token in key.upper() for token in ("TOKEN", "API_KEY", "SECRET", "PASSWORD", "CREDENTIAL"))}
        env.update(COMPANION_ENGINE_HOME=str(home))
        if command([str(cli), "version"], env=env) != version(tag):
            raise ValueError("Headless binary does not report the reviewed version")
        process = None
        try:
            with (Path(temporary) / "desktop.log").open("wb") as log:
                process = subprocess.Popen([str(desktop)], env=env, stdout=log, stderr=subprocess.STDOUT)
                deadline = time.monotonic() + 45
                handle = None
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError("Packaged desktop exited before displaying its window")
                    handle = window_for(process.pid)
                    if handle:
                        try:
                            command([str(cli), "threads", "--home", str(home)], env=env, timeout=5)
                            break
                        except (RuntimeError, subprocess.TimeoutExpired):
                            pass
                    time.sleep(0.2)
                else:
                    raise RuntimeError("Packaged window and authenticated engine did not become ready")
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                if not user32.PostMessageW(handle, 0x0010, 0, 0):  # WM_CLOSE, owned window only.
                    raise RuntimeError("Packaged test window could not close")
                if process.wait(timeout=20) != 0:
                    raise RuntimeError("Packaged desktop reported failure when closing")
                # The closed window must leave its resident engine usable.
                command([str(cli), "threads", "--home", str(home)], env=env)
                command([str(cli), "down", "--home", str(home)], env=env)
                stop_deadline = time.monotonic() + 10
                while time.monotonic() < stop_deadline:
                    try:
                        command([str(cli), "threads", "--home", str(home)], env=env, timeout=2)
                    except (RuntimeError, subprocess.TimeoutExpired):
                        break
                    time.sleep(0.1)
                else:
                    raise RuntimeError("The isolated test engine did not stop")
        finally:
            # Cleanup is scoped by the private test home, never port scanning or
            # process-name matching. The desktop process is the one we created.
            try:
                command([str(cli), "down", "--home", str(home)], env=env, timeout=10)
            except Exception:
                pass
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
    print("Windows packaged checks passed: exact version, visible window, authenticated engine, residency and shutdown")
