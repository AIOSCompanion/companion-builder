"""Exercise the real sealed-output wrapper with a disposable key and public synthetic output only."""
from __future__ import annotations

import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from run_sealed import run_step


def main():
    gpg = shutil.which("gpg")
    if not gpg and os.name == "nt":
        candidate = Path("C:/Program Files/Git/usr/bin/gpg.exe")
        if candidate.is_file():
            gpg = str(candidate)
    if not gpg:
        print("gpg smoke: gpg is unavailable")
        return 1
    old_home = os.environ.get("GNUPGHOME")
    with tempfile.TemporaryDirectory(prefix="builder-public-smoke-") as temp:
        home = Path(temp) / "keyring"
        home.mkdir(mode=0o700)
        os.environ["GNUPGHOME"] = str(home)
        try:
            generated = subprocess.run([gpg, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                                        "--quick-generate-key", "builder-public-self-check", "rsa2048", "encr", "1d"],
                                       capture_output=True, check=False)
            if generated.returncode:
                raise RuntimeError("disposable encryption key generation failed")
            summary = io.StringIO()
            logs = Path(temp) / "sealed"
            result = run_step("public-smoke", [sys.executable, "-c", "print('builder-public-smoke')"],
                              log_dir=logs, recipient="builder-public-self-check", gpg=(gpg,), out=summary)
            if result or "builder-public-smoke" in summary.getvalue():
                raise RuntimeError("sealed wrapper failed or exposed child output")
            sealed = logs / "public-smoke.log.gpg"
            opened = subprocess.run([gpg, "--batch", "--decrypt", str(sealed)], capture_output=True, check=False)
            if opened.returncode or opened.stdout.strip() != b"builder-public-smoke":
                raise RuntimeError("encrypted output did not round-trip")
            print("gpg smoke: sealed output round-trips; public summary does not contain child output")
            return 0
        except RuntimeError as error:
            print(f"gpg smoke: {error}")
            return 1
        finally:
            gpgconf = shutil.which("gpgconf") or str(Path(gpg).with_name("gpgconf.exe" if os.name == "nt" else "gpgconf"))
            if Path(gpgconf).is_file():
                subprocess.run([gpgconf, "--kill", "all"], capture_output=True, check=False)
            if old_home is None:
                os.environ.pop("GNUPGHOME", None)
            else:
                os.environ["GNUPGHOME"] = old_home


if __name__ == "__main__":
    sys.exit(main())
