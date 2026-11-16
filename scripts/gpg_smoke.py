"""Exercise the real sealed-output wrapper with a disposable key and public synthetic output only."""
from __future__ import annotations

import io
import contextlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import shlex

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_sealed import run_step, gpg_filename


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
    # Short hosted paths avoid gpg-agent socket-length failures on macOS and Windows.
    with tempfile.TemporaryDirectory(prefix="gpg-", dir=os.environ.get("RUNNER_TEMP")) as temp:
        home = Path(temp) / "keyring"
        home.mkdir(mode=0o700)
        os.environ["GNUPGHOME"] = gpg_filename((gpg,), home)
        try:
            generated = subprocess.run([gpg, "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
                                        "--quick-generate-key", "builder-public-self-check", "rsa2048", "encr", "1d"],
                                       capture_output=True, check=False)
            if generated.returncode:
                # This process only generated a disposable synthetic key. No owner key/config is involved.
                print(generated.stderr.decode("utf-8", errors="replace")[-1800:])
                raise RuntimeError("disposable encryption key generation failed")
            summary = io.StringIO()
            logs = Path(temp) / "sealed"
            junit = Path(temp) / "junit.xml"
            xml = '<testsuite tests="1" failures="0" errors="0" skipped="0"/>'
            command = f"from pathlib import Path; print('builder-public-smoke'); Path({str(junit)!r}).write_text({xml!r})"
            result = run_step("public-smoke", [sys.executable, "-c", command],
                              log_dir=logs, recipient="builder-public-self-check", gpg=(gpg,), out=summary, junit=junit)
            if result or "builder-public-smoke" in summary.getvalue():
                raise RuntimeError("sealed wrapper failed or exposed child output")
            sealed = logs / "public-smoke.log.gpg"
            opened = subprocess.run([gpg, "--batch", "--decrypt", gpg_filename((gpg,), sealed)], capture_output=True, check=False)
            if opened.returncode or opened.stdout.strip() != b"builder-public-smoke":
                raise RuntimeError("encrypted output did not round-trip")
            report = subprocess.run([gpg, "--batch", "--decrypt", gpg_filename((gpg,), logs / "public-smoke-junit.xml.gpg")],
                                    capture_output=True, check=False)
            if report.returncode or report.stdout != xml.encode():
                raise RuntimeError("encrypted report did not round-trip")
            if sys.platform == "linux":
                timed = run_step("public-timeout", ["/usr/bin/timeout", "--signal=TERM", "--kill-after=2s", "1s",
                                 sys.executable, "-c", "import time; print('public-timeout-marker', flush=True); time.sleep(30)"],
                                 log_dir=logs, recipient="builder-public-self-check", gpg=(gpg,), out=summary)
                opened = subprocess.run([gpg, "--batch", "--decrypt", str(logs / "public-timeout.log.gpg")],
                                        capture_output=True, check=False)
                if timed != 124 or opened.returncode or opened.stdout.strip() != b"public-timeout-marker":
                    raise RuntimeError("timed-out output did not seal completely")
                if "public-timeout-marker" in summary.getvalue():
                    raise RuntimeError("timed-out output escaped the sealer")
            if "--suite-guard" in sys.argv:
                from run_suite import main as run_suite
                # A detached helper deliberately retains stdout. Without the
                # UID cleanup, the sealer waits for its 30-second sleep.
                child = "import time; time.sleep(30)"
                code = (f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}], "
                        "start_new_session=True); print('public-orphan-marker',flush=True)")
                started = time.monotonic()
                def capture(argv, *, env, check):
                    result = subprocess.run(argv, env=env, check=check, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                    summary.write(result.stdout.decode("utf-8", errors="replace"))
                    return result
                with contextlib.redirect_stdout(summary):
                    guarded = run_suite(run=capture, env={**os.environ, "SUITE_NAME": "public-orphan-guard",
                                            "TEST_COMMAND": shlex.join([sys.executable, "-c", code]),
                                            "LOG_RECIPIENT": "builder-public-self-check", "SEALED_LOG_DIR": str(logs),
                                            "SUITE_TIMEOUT_SECONDS": "5"})
                opened = subprocess.run([gpg, "--batch", "--decrypt", str(logs / "public-orphan-guard.log.gpg")],
                                        capture_output=True, check=False)
                if guarded or time.monotonic() - started > 15 or opened.returncode or opened.stdout.strip() != b"public-orphan-marker":
                    raise RuntimeError("detached test helper prevented clean suite shutdown")
                if "public-orphan-marker" in summary.getvalue():
                    raise RuntimeError("detached helper output escaped the sealer")
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
