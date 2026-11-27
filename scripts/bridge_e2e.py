#!/usr/bin/env python3
"""End-to-end proof on a clean Windows runner: the previous Electron Companion (0.18.0) takes the bridge through its own updater.

Runs from the trusted snapshot (python -I -B), after the bridge is built and before the draft is created, and only inside
run_sealed.py, so everything it prints and writes is sealed to the team log key. It reads the bridge outputs from --bridge-dir
and publishes nothing. The only network reads are the public 0.18.0 installer and its release record, and the bridge feed is
served from this machine on 127.0.0.1.

The steps, in order:
  1. Install the public 0.18.0 installer silently (/S) into %LOCALAPPDATA%\\Programs\\Companion.
  2. Seed the previous app's data folder with the two stores the engine looks for, in the folder the previous app writes.
  3. Point the installed resources\\app-update.yml at the local feed (provider: generic). This is a deviation from the
     shipped GitHub feed, needed because the runner must not install the bridge from the public release.
  4. Start the previous app with a local debugging port, so its own updater runs. Its updater downloads only on a click,
     so the driver clicks "Update Now" and then "Restart Now", the same two controls a person uses.
  5. Wait for the bridge's installer to finish (its takeover log, from a pre-run offset), then start the Go app.
  6. Check the result (CHECKS below). The import waits for a sign-in that a runner cannot give, so the import is verified
     as pending, not finished.

Exit code 0 only when every check passes. The workflow creates the draft only after that.
"""
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_release  # noqa: E402  (same folder: the bridge's own output checks)

OLD_VERSION = "0.18.0"
OLD_RELEASE_REPO = "AIOSCompanion/aios-companion-releases"
OLD_INSTALLER = "Companion-Setup-x64-{v}.exe"
UNINSTALL_ROOT = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
BRIDGE_UNINSTALL_GUID = "abae653c-89e1-575a-aa38-9a03a1b8a211"
PROGRAM = "Companion.exe"
CLI = "companion-cli.exe"
UNINSTALLER = "Uninstall Companion.exe"
RECEIPT = "companion-install.json"
PREVIOUS_RECORD = "takeover-previous.json"
TAKEOVER_LOG = "takeover.log"
IMPORT_FINISHED = "preview import finished"
USER_AGENT = "companion-builder-e2e"
DRIVER = "bridge_e2e_cdp.mjs"
NEW_APP_SECONDS = 60
OLD_APP_SECONDS = 900
BRIDGE_SECONDS = 900
INSTALL_SECONDS = 900
CLI_SECONDS = 60

SCREENSHOT_SCRIPT = r"""
param([string]$Path)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bitmap = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bitmap.Size)
$bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
$graphics.Dispose()
$bitmap.Dispose()
"""


class E2EError(Exception):
    """A step that cannot continue. The message names the step and nothing from the machine's files."""


class Run:
    """The run's own record: a plain log and the checks, written to the sealed output folder."""

    def __init__(self, out: Path):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.log_path = self.out / "e2e-log.txt"
        self.checks: list[dict] = []

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def check(self, name: str, ok: bool, detail: str) -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail})
        self.log(f"check {name}: {'PASS' if ok else 'FAIL'} - {detail}")
        return bool(ok)

    def passed(self) -> bool:
        return bool(self.checks) and all(item["ok"] for item in self.checks)

    def write_result(self) -> None:
        document = {"passed": self.passed(), "checks": self.checks}
        (self.out / "e2e-result.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


# ---- Pure parts (tested on any host) -------------------------------------------------------------------------------------

def point_feed_at(text: str, url: str) -> str:
    """The installed app-update.yml with its provider replaced by the local generic feed. Other keys are kept as written."""
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{2,5}", url):
        raise E2EError("the feed must be the local 127.0.0.1 server")
    kept = [line for line in text.splitlines()
            if line.strip() and not re.match(r"^(provider|owner|repo|url|channel|host)\s*:", line)]
    return "\n".join(["provider: generic", f"url: {url}", *kept]) + "\n"


def old_data_dir(app_data: Path) -> Path:
    """The previous app's data folder on Windows: %APPDATA%\\Companion\\aegra-local (the engine's legacyDataDirOn)."""
    return Path(app_data) / "Companion" / "aegra-local"


def seed_paths(app_data: Path) -> tuple[Path, Path]:
    """The two stores the engine reads: the detection file eventlog\\events.db, and companion.db."""
    base = old_data_dir(app_data)
    return base / "eventlog" / "events.db", base / "companion.db"


def seed_old_data(app_data: Path) -> list[Path]:
    """Writes the previous app's stores with made-up content. The table layout is a stand-in: the file names and folders are
    the previous app's, the schema is not, and the import is only checked as pending."""
    events, companion = seed_paths(app_data)
    events.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(events)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)")
        db.execute("INSERT INTO events (kind, payload) VALUES (?, ?)", ("e2e.seed", "synthetic row for the bridge e2e"))
        db.commit()
    with contextlib.closing(sqlite3.connect(companion)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("e2e.seed", "synthetic"))
        db.commit()
    return [events, companion]


def check_stable_install(stable: Path, version: str, cli_output: str | None) -> tuple[bool, str]:
    receipt = Path(stable) / RECEIPT
    if not receipt.is_file():
        return False, "no install receipt at the stable path"
    try:
        found = json.loads(receipt.read_text(encoding="utf-8")).get("version")
    except (ValueError, OSError) as error:
        return False, f"the install receipt does not read: {type(error).__name__}"
    if found != version:
        return False, f"receipt version {found!r}, expected {version!r}"
    if cli_output is not None and cli_output.strip() != version:
        return False, f"companion-cli version printed {cli_output.strip()!r}, expected {version!r}"
    return True, f"receipt and companion-cli both report {version}"


def check_old_app_gone(stable: Path) -> tuple[bool, str]:
    leftovers = [name for name in (UNINSTALLER, os.path.join("resources", "app.asar"),
                                   os.path.join("resources", "app-update.yml")) if (Path(stable) / name).exists()]
    if leftovers:
        return False, f"previous-app files still at the stable path: {leftovers}"
    return True, "no previous-app program files at the stable path"


def _norm(value) -> str:
    return os.path.normcase(os.path.normpath(str(value or "")))


def check_uninstall(entries: list[tuple[str, dict]], version: str, stable: Path) -> tuple[bool, str]:
    """entries are the per-user uninstall keys as (key name, values). The bridge's key must name this version and folder, and
    no other key may still name Companion."""
    problems = []
    mine = [values for key, values in entries if key.lower() == BRIDGE_UNINSTALL_GUID]
    if not mine:
        problems.append("the bridge's uninstall key is missing")
    else:
        values = mine[0]
        if values.get("DisplayVersion") != version:
            problems.append(f"DisplayVersion is {values.get('DisplayVersion')!r}, expected {version!r}")
        if _norm(values.get("InstallLocation")) != _norm(stable):
            problems.append("InstallLocation is not the stable folder")
        if "--uninstall" not in str(values.get("UninstallString", "")):
            problems.append("UninstallString does not start the program's uninstall")
    for key, values in entries:
        if key.lower() == BRIDGE_UNINSTALL_GUID:
            continue
        if "companion" in str(values.get("DisplayName", "")).lower():
            problems.append(f"another Companion entry remains ({key}, {values.get('DisplayVersion')!r})")
    if problems:
        return False, "; ".join(problems)
    return True, f"one Companion entry, naming {version} and the stable folder"


def check_update_path(takeover_text: str) -> tuple[bool, str]:
    """The bridge ran as an update started by the previous app (--updated), and finished with exit 0."""
    if not re.search(r"start: args=.*--updated", takeover_text):
        return False, "the bridge did not start with --updated"
    if "install complete" not in takeover_text:
        return False, "the bridge log has no 'install complete'"
    if not re.search(r"\bexit 0\b", takeover_text):
        return False, "the bridge log has no 'exit 0'"
    return True, "started by the previous app with --updated, install complete, exit 0"


def check_feed_served(records: list[tuple[str, str]], installer_name: str) -> tuple[bool, str]:
    latest = any(path.endswith("/latest.yml") and code == "200" for path, code in records)
    installer = any(path.endswith("/" + installer_name) and code in ("200", "206") for path, code in records)
    if not latest:
        return False, "the previous app never fetched latest.yml from the local feed"
    if not installer:
        return False, "the previous app never fetched the installer from the local feed"
    return True, "the previous app fetched latest.yml and the installer from the local feed"


def check_old_data_detected(app_data: Path) -> tuple[bool, str]:
    events, companion = seed_paths(app_data)
    ok = events.is_file() and companion.is_file()
    return ok, "eventlog\\events.db and companion.db are in the previous app's folder" if ok else "the previous app's stores are missing"


def check_import_pending(engine_home: Path, launched_at: float, previous_record: Path, old_data_present: bool,
                         logs_text: str) -> tuple[bool, str]:
    """Pending, not finished: the Go app wrote under its home after launch, the import never reached its finish line, the
    previous app's folder the takeover kept is still there, and the previous app's data is still in place."""
    engine_home = Path(engine_home)
    wrote = engine_home.is_dir() and any(p.is_file() and p.stat().st_mtime >= launched_at for p in engine_home.rglob("*"))
    if not wrote:
        return False, "the Go app wrote nothing under its home after launch, so the import state is unknown"
    if IMPORT_FINISHED in logs_text:
        return False, "the import reached its finish line, but a runner cannot give the sign-in it needs"
    kept = []
    if previous_record.is_file():
        try:
            kept = [Path(item["previous"]) for item in json.loads(previous_record.read_text(encoding="utf-8")).get("installs", [])]
        except (ValueError, KeyError, TypeError):
            return False, "the takeover's previous-install record does not read"
    if not kept or not all(folder.is_dir() for folder in kept):
        return False, "the previous install folder the takeover keeps for the import is not there"
    if not old_data_present:
        return False, "the previous app's data was removed"
    return True, "pending: no finish line, previous install folder and previous data kept (inferred from these; no sign-in on a runner)"


def check_cdp_driver_result(returncode: int) -> tuple[bool, str]:
    return returncode == 0, "Restart Now was clicked" if returncode == 0 else f"the update driver exited {returncode}"


def read_logs_text(engine_home: Path, limit: int = 20 * 1024 * 1024) -> str:
    """The engine's own text files (logs and records), read as text. Binary stores are skipped by suffix."""
    parts = []
    if not Path(engine_home).is_dir():
        return ""
    for path in Path(engine_home).rglob("*"):
        if path.is_file() and path.suffix.lower() in (".log", ".txt", ".jsonl", ".json") and path.stat().st_size <= limit:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


# ---- Windows and network parts (run only on the runner) -------------------------------------------------------------

def read_companion_entries() -> list[tuple[str, dict]]:
    """Every per-user uninstall key whose subkey name or display name mentions Companion."""
    import winreg
    entries = []
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_ROOT) as root:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            with winreg.OpenKey(root, name) as key:
                values, slot = {}, 0
                while True:
                    try:
                        value_name, value, _ = winreg.EnumValue(key, slot)
                    except OSError:
                        break
                    values[value_name] = value
                    slot += 1
            if name.lower() == BRIDGE_UNINSTALL_GUID or "companion" in str(values.get("DisplayName", "")).lower():
                entries.append((name, values))
    return entries


def bridge_uninstall_key_exists() -> bool:
    import winreg
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{UNINSTALL_ROOT}\\{BRIDGE_UNINSTALL_GUID}").Close()
        return True
    except FileNotFoundError:
        return False


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _FeedHandler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".yml": "text/yaml", ".blockmap": "application/octet-stream",
                      ".exe": "application/octet-stream"}
    records: list[tuple[str, str]] = []

    def log_message(self, format, *args):  # noqa: A002  (the base class's signature)
        return

    def log_request(self, code="-", size="-"):
        self.records.append((self.path, str(code)))


def serve_feed(directory: Path):
    """A static server on 127.0.0.1 for the feed folder. Returns (server, base url, the request records)."""
    records: list[tuple[str, str]] = []

    class Handler(_FeedHandler):
        pass

    Handler.records = records
    handler = functools.partial(Handler, directory=str(directory))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", records


def fetch_old_installer(work: Path, token: str) -> Path:
    """The public 0.18.0 installer, checked against the digest its release record gives for it."""
    name = OLD_INSTALLER.format(v=OLD_VERSION)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    api = f"https://api.github.com/repos/{OLD_RELEASE_REPO}/releases/tags/v{OLD_VERSION}"
    with urllib.request.urlopen(urllib.request.Request(api, headers=headers), timeout=60) as response:
        release = json.load(response)
    asset = next((item for item in release.get("assets", []) if item.get("name") == name), None)
    if asset is None or not str(asset.get("digest", "")).startswith("sha256:"):
        raise E2EError("the 0.18.0 release has no digest for its installer")
    target = work / name
    digest = hashlib.sha256()
    request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=600) as response, target.open("wb") as handle:
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
            handle.write(chunk)
    if asset["digest"] != "sha256:" + digest.hexdigest():
        raise E2EError("the 0.18.0 installer does not match its recorded digest")
    return target


def wait_for_http(url: str, seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return True
        except OSError:
            time.sleep(2)
    return False


def read_from(path: Path, offset: int) -> str:
    if not path.is_file():
        return ""
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read().decode("utf-8", errors="replace")


def wait_for_takeover(log_path: Path, offset: int, seconds: int) -> str:
    """Waits for the bridge's own 'exit N' line, after the pre-run offset, and returns the text of this run only."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        text = read_from(log_path, offset)
        if re.search(r"\bexit \d+\b", text):
            return text
        time.sleep(3)
    raise E2EError("the bridge did not finish in time")


def capture(out: Path, name: str, script: Path, run: Run) -> None:
    target = out / f"{name}.png"
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Path", str(target)],
                            capture_output=True, text=True, timeout=120, check=False)
    if result.returncode or not target.is_file():
        run.log(f"screenshot {name} was not taken (exit {result.returncode})")
    else:
        run.log(f"screenshot {name} taken")


def stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)


# ---- The run ------------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True, help="the Go release tag the bridge was built from (vX.Y.Z)")
    parser.add_argument("--bridge-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    run = Run(args.out)
    servers, procs = [], []
    try:
        version = bridge_release.bridge_version(args.tag)
        files = bridge_release.verify_outputs(args.bridge_dir, version)
        installer_name = bridge_release.expected_names(version, bridge_release.BRIDGE_ASSETS)[0]
        if installer_name not in files:
            raise E2EError("the bridge output has no installer")
        local = Path(os.environ["LOCALAPPDATA"])
        roaming = Path(os.environ["APPDATA"])
        stable = local / "Programs" / "Companion"
        engine_home = local / "companion-engine"
        takeover_log = engine_home / "logs" / TAKEOVER_LOG
        work = Path(tempfile.mkdtemp(prefix="bridge-e2e-"))
        feed = work / "feed"
        feed.mkdir()
        for path in files.values():
            shutil.copy2(path, feed / path.name)
        run.log(f"bridge {version} verified; feed prepared")

        fresh = (not stable.exists() and not (roaming / "Companion").exists() and not bridge_uninstall_key_exists())
        run.check("fresh_runner", fresh, "no previous or bridge install, data or uninstall key before the run")

        old_installer = fetch_old_installer(work, os.environ.get("E2E_GITHUB_TOKEN", ""))
        run.log("0.18.0 installer fetched and digest-checked")
        code = subprocess.run([str(old_installer), "/S"], timeout=INSTALL_SECONDS, check=False).returncode
        run.check("old_install_silent", code == 0, f"0.18.0 silent install exit {code}")
        old_entries = [v for k, v in read_companion_entries() if k.lower() == BRIDGE_UNINSTALL_GUID]
        run.check("old_install_registered", bool(old_entries) and old_entries[0].get("DisplayVersion") == OLD_VERSION,
                  "the uninstall key names 0.18.0" if old_entries else "no uninstall key after the 0.18.0 install")

        seed_old_data(roaming)
        run.check("old_data_seeded", check_old_data_detected(roaming)[0], "the previous app's stores were written")

        app_update = stable / "resources" / "app-update.yml"
        if not app_update.is_file():
            raise E2EError("the 0.18.0 install has no resources\\app-update.yml")
        server, base_url, records = serve_feed(feed)
        servers.append(server)
        app_update.write_text(point_feed_at(app_update.read_text(encoding="utf-8"), base_url), encoding="utf-8")
        run.log("installed app-update.yml now points at the local feed (provider: generic)")

        script = work / "shot.ps1"
        script.write_text(SCREENSHOT_SCRIPT, encoding="utf-8")
        capture(args.out, "01-old-app-before-update", script, run)

        offset = takeover_log.stat().st_size if takeover_log.is_file() else 0
        port = free_port()
        old_proc = subprocess.Popen([str(stable / PROGRAM), f"--remote-debugging-port={port}"], cwd=str(stable))
        procs.append(old_proc)
        if not wait_for_http(f"http://127.0.0.1:{port}/json/version", 120):
            raise E2EError("the previous app did not open its debugging port")
        driver = Path(__file__).resolve().parent / DRIVER
        driven = subprocess.run(["node", str(driver), str(port), str(OLD_APP_SECONDS)], capture_output=True, text=True,
                                timeout=OLD_APP_SECONDS + 120, check=False)
        (args.out / "cdp-driver.txt").write_text(driven.stdout + driven.stderr, encoding="utf-8")
        ok, detail = check_cdp_driver_result(driven.returncode)
        run.check("old_app_restart_clicked", ok, detail)

        try:
            old_proc.wait(timeout=OLD_APP_SECONDS)
            old_quit = True
        except subprocess.TimeoutExpired:
            old_quit = False
        run.check("old_app_quit_for_install", old_quit, "the previous app quit to hand off to the installer" if old_quit
                  else "the previous app did not quit")

        takeover_text = wait_for_takeover(takeover_log, offset, BRIDGE_SECONDS)
        (args.out / "takeover-run.txt").write_text(takeover_text, encoding="utf-8")
        ok, detail = check_update_path(takeover_text)
        run.check("bridge_ran_from_old_updater", ok, detail)
        ok, detail = check_feed_served(records, installer_name)
        run.check("feed_served_the_bridge", ok, detail)

        launched_at = time.time()
        new_proc = subprocess.Popen([str(stable / PROGRAM)], cwd=str(stable))
        procs.append(new_proc)
        time.sleep(NEW_APP_SECONDS)
        run.check("go_app_launches", new_proc.poll() is None and engine_home.is_dir(),
                  "the Go app is still running after 60 seconds and its home exists" if new_proc.poll() is None
                  else f"the Go app exited with {new_proc.returncode}")
        capture(args.out, "02-go-app-after-update", script, run)

        cli_output = None
        if (stable / CLI).is_file():
            cli = subprocess.run([str(stable / CLI), "version"], capture_output=True, text=True, timeout=CLI_SECONDS, check=False)
            cli_output = cli.stdout if cli.returncode == 0 else None
        ok, detail = check_stable_install(stable, version, cli_output)
        run.check("stable_path_has_go_version", ok, detail)
        ok, detail = check_old_app_gone(stable)
        run.check("old_app_gone", ok, detail)
        ok, detail = check_uninstall(read_companion_entries(), version, stable)
        run.check("no_stale_uninstall_entry", ok, detail)
        ok, detail = check_import_pending(engine_home, launched_at, engine_home / PREVIOUS_RECORD,
                                          check_old_data_detected(roaming)[0], read_logs_text(engine_home))
        run.check("import_pending_not_finished", ok, detail)
        run.check("old_data_detected", check_old_data_detected(roaming)[0], "the previous app's stores are where the engine looks")
        return 0 if run.passed() else 1
    except (E2EError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        run.log(f"stopped: {type(error).__name__}: {error}")
        run.check("e2e_completed", False, f"stopped before the checks finished: {type(error).__name__}")
        return 1
    finally:
        for proc in procs:
            stop(proc)
        for server in servers:
            server.shutdown()
            server.server_close()
        run.write_result()


if __name__ == "__main__":
    sys.exit(main())
