"""The shared sealing wrapper (scripts/run_sealed.py).

The public log must show only a step's name, exit code, duration and error class. These tests use a
fake command that prints a private path and a fake secret, and check that neither reaches the
public stream while both are sealed in the log. A fake gpg stands in for the team key, so the
sealed file's contents can be read in the test.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("run_sealed", ROOT / "scripts" / "run_sealed.py")
run_sealed = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_sealed)

PATH = "C:/work/private-companion/src/secret_module.py"
SECRET = "fixture-secret-0001"

FAKE_GPG = '''
import sys
args = sys.argv[1:]
target = args[args.index("-o") + 1]
source = args[-1]
if source == target:
    data = sys.stdin.buffer.read()
else:
    with open(source, "rb") as stream:
        data = stream.read()
with open(target, "wb") as stream:
    stream.write(b"CIPHER:" + data)
'''

FAKE_GPG_FAILS = '''
import sys
sys.exit(2)
'''


class SealedStepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="run sealed ")
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.gpg_path = self.dir / "fake_gpg.py"
        self.gpg_path.write_text(FAKE_GPG, encoding="utf-8")
        self.gpg = (sys.executable, str(self.gpg_path))
        self.logs = self.dir / "logs"

    def command(self, body):
        return [sys.executable, "-c", body]

    def leaky_failure(self):
        return self.command(
            f'import sys; print("{PATH}"); print("{SECRET}"); '
            f'sys.stderr.write("{SECRET} at {PATH}\\n"); raise ValueError("boom")')

    def test_path_and_secret_stay_out_of_the_public_stream_but_are_sealed(self):
        out = io.StringIO()
        code = run_sealed.run_step("sidecars", self.leaky_failure(), log_dir=self.logs,
                                   recipient="team-key", gpg=self.gpg, out=out)
        public = out.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn(PATH, public)
        self.assertNotIn(SECRET, public)
        self.assertIn("step sidecars: exit 1 in", public)
        self.assertIn("last error class: ValueError", public)
        sealed = (self.logs / "sidecars.log.gpg").read_bytes()
        self.assertTrue(sealed.startswith(b"CIPHER:"))
        self.assertIn(PATH.encode(), sealed)
        self.assertIn(SECRET.encode(), sealed)

    def test_a_clean_step_prints_exit_zero_and_no_error_class(self):
        out = io.StringIO()
        code = run_sealed.run_step("android", self.command('print("quiet")'), log_dir=self.logs,
                                   recipient="team-key", gpg=self.gpg, out=out)
        self.assertEqual(code, 0)
        self.assertIn("step android: exit 0 in", out.getvalue())
        self.assertNotIn("last error class", out.getvalue())
        self.assertIn(b"quiet", (self.logs / "android.log.gpg").read_bytes())

    def test_no_recipient_refuses_and_the_command_never_runs(self):
        marker = self.dir / "ran"
        body = f'open(r"{marker}", "w").close()'
        with self.assertRaisesRegex(run_sealed.RefusedError, "No recipient"):
            run_sealed.run_step("x", self.command(body), log_dir=self.logs, recipient="", gpg=self.gpg,
                                out=io.StringIO())
        self.assertFalse(marker.exists())

    def test_a_missing_gpg_refuses_and_the_command_never_runs(self):
        marker = self.dir / "ran"
        body = f'open(r"{marker}", "w").close()'
        with self.assertRaisesRegex(run_sealed.RefusedError, "gpg could not start"):
            run_sealed.run_step("x", self.command(body), log_dir=self.logs, recipient="team-key",
                                gpg=("no-such-gpg-binary-xyz",), out=io.StringIO())
        self.assertFalse(marker.exists())

    def test_a_failed_seal_is_reported_and_fails_the_step(self):
        failing = self.dir / "fail_gpg.py"
        failing.write_text(FAKE_GPG_FAILS, encoding="utf-8")
        out = io.StringIO()
        code = run_sealed.run_step("x", self.command('print("hi")'), log_dir=self.logs, recipient="team-key",
                                   gpg=(sys.executable, str(failing)), out=out)
        self.assertEqual(code, 1)
        self.assertIn("could not be sealed", out.getvalue())

    def test_junit_prints_counts_only_and_seals_the_names(self):
        junit = self.dir / "junit.xml"
        junit.write_text(
            f'<testsuites><testsuite><testcase classname="{PATH}" name="{SECRET}">'
            f'<failure message="m">trace</failure></testcase>'
            f'<testcase classname="x" name="ok"/></testsuite></testsuites>', encoding="utf-8")
        out = io.StringIO()
        code = run_sealed.run_step("engine tests", self.command('print("ran")'), log_dir=self.logs,
                                   recipient="team-key", gpg=self.gpg, junit=str(junit), out=out)
        public = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("tests 2, passed 1, failed 1, errors 0, skipped 0; suite exit 0", public)
        self.assertNotIn(SECRET, public)
        self.assertNotIn(PATH, public)
        # The junit file was written by untrusted code. The wrapper never removes it: it seals a staged copy,
        # and the workflow removes the original. No staged copy is left in the log folder.
        self.assertTrue(junit.exists())
        self.assertEqual(list(self.logs.glob(".*staged")), [])
        self.assertIn(SECRET.encode(), (self.logs / "engine-tests-junit.xml.gpg").read_bytes())

    def test_a_malformed_junit_is_reported_without_its_text(self):
        junit = self.dir / "junit.xml"
        junit.write_text(f"<testsuites {SECRET}", encoding="utf-8")
        out = io.StringIO()
        run_sealed.run_step("x", self.command('print("hi")'), log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, junit=str(junit), out=out)
        self.assertIn("junit report unreadable", out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())

    def test_sealed_directories_are_sealed_file_by_file_and_plaintext_removed(self):
        evidence = self.dir / "evidence"
        (evidence / "nested").mkdir(parents=True)
        (evidence / "a.json").write_text(SECRET, encoding="utf-8")
        (evidence / "nested" / "b.log").write_text(PATH, encoding="utf-8")
        out = io.StringIO()
        run_sealed.run_step("packaging", self.command('print("ok")'), log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, seal_dirs=[str(evidence)], out=out)
        self.assertFalse((evidence / "a.json").exists())
        self.assertFalse((evidence / "nested" / "b.log").exists())
        self.assertIn(SECRET.encode(), (self.logs / "packaging-evidence" / "a.json.gpg").read_bytes())
        self.assertIn(PATH.encode(), (self.logs / "packaging-evidence" / "nested" / "b.log.gpg").read_bytes())
        self.assertNotIn(SECRET, out.getvalue())

    def test_the_child_cannot_publish_through_the_summary_or_the_workflow_commands(self):
        # A child writing to GITHUB_STEP_SUMMARY, printing a workflow command, or holding the runtime token
        # must leave no trace in the public stream or the summary file.
        summary = self.dir / "step-summary.md"
        summary.write_text("", encoding="utf-8")
        body = (f'import os\n'
                f'p = os.environ.get("GITHUB_STEP_SUMMARY")\n'
                f'if p: open(p, "a").write("SUMMARY-LEAK " + "{SECRET}")\n'
                f'print("::warning::" + "{SECRET}")\n'
                f'print("TOKEN_PRESENT=" + str("ACTIONS_RUNTIME_TOKEN" in os.environ))\n')
        base = dict(os.environ, GITHUB_STEP_SUMMARY=str(summary), ACTIONS_RUNTIME_TOKEN="runtime-token-value",
                    ACTIONS_ID_TOKEN_REQUEST_URL="https://example.invalid/token")
        out = io.StringIO()
        run_sealed.run_step("side channels", self.command(body), log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, out=out, base_env=base)
        public = out.getvalue()
        self.assertNotIn("SUMMARY-LEAK", summary.read_text(encoding="utf-8"))
        self.assertNotIn(SECRET, public)
        self.assertNotIn("::warning::", public)
        self.assertNotIn("runtime-token-value", public)
        sealed = (self.logs / "side-channels.log.gpg").read_bytes()
        self.assertIn(b"TOKEN_PRESENT=False", sealed)

    def test_file_outputs_are_written_to_the_real_files_by_the_wrapper(self):
        real_env = self.dir / "github-env"
        real_output = self.dir / "github-output"
        real_env.write_text("", encoding="utf-8")
        real_output.write_text("", encoding="utf-8")
        body = (f'import os\n'
                f'open(os.environ["GITHUB_ENV"], "a").write("FROM_CHILD=1\\n")\n'
                f'open(os.environ["GITHUB_OUTPUT"], "a").write("out=2\\n")\n'
                f'print("CHILD_HAS_REAL_ENV_FILE=" + str(os.environ["GITHUB_ENV"] == r"{real_env}"))\n')
        base = dict(os.environ, GITHUB_ENV=str(real_env), GITHUB_OUTPUT=str(real_output))
        out = io.StringIO()
        code = run_sealed.run_step("file outputs", self.command(body), log_dir=self.logs, recipient="team-key",
                                   gpg=self.gpg, out=out, base_env=base,
                                   export_env=("FROM_CHILD",), export_output=("out",))
        self.assertEqual(code, 0)
        self.assertIn("FROM_CHILD=1", real_env.read_text(encoding="utf-8"))
        self.assertIn("out=2", real_output.read_text(encoding="utf-8"))
        self.assertIn(b"CHILD_HAS_REAL_ENV_FILE=False", (self.logs / "file-outputs.log.gpg").read_bytes())

    def test_an_untrusted_child_cannot_set_node_options_or_a_path_without_the_flag(self):
        # The PR tests are untrusted: with no --export-env/--export-output, the child has no GITHUB_ENV,
        # GITHUB_OUTPUT or GITHUB_PATH at all. Nothing it writes can reach a later step.
        real = {name: self.dir / name.lower() for name in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH")}
        for path in real.values():
            path.write_text("", encoding="utf-8")
        body = (f'import os\n'
                f'for name, line in (("GITHUB_ENV", "NODE_OPTIONS=--require=/tmp/x.js\\n"),\n'
                f'                    ("GITHUB_PATH", "/tmp/evil-bin\\n"),\n'
                f'                    ("GITHUB_OUTPUT", "out=poison\\n")):\n'
                f'    if os.environ.get(name):\n'
                f'        open(os.environ[name], "a").write(line)\n'
                f'print("ENV_SEEN=" + str(any(k in os.environ for k in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_PATH"))))\n')
        base = dict(os.environ, GITHUB_ENV=str(real["GITHUB_ENV"]), GITHUB_OUTPUT=str(real["GITHUB_OUTPUT"]),
                    GITHUB_PATH=str(real["GITHUB_PATH"]))
        out = io.StringIO()
        run_sealed.run_step("untrusted", self.command(body), log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, out=out, base_env=base)
        for path in real.values():
            self.assertEqual(path.read_text(encoding="utf-8"), "", path.name)
        self.assertIn(b"ENV_SEEN=False", (self.logs / "untrusted.log.gpg").read_bytes())

    def test_with_the_flag_only_the_allowlisted_name_comes_back(self):
        real_env = self.dir / "github-env"
        real_env.write_text("", encoding="utf-8")
        body = (f'import os\n'
                f'open(os.environ["GITHUB_ENV"], "a").write("FOO=1\\nNODE_OPTIONS=--require=/tmp/x.js\\n")\n')
        base = dict(os.environ, GITHUB_ENV=str(real_env))
        out = io.StringIO()
        run_sealed.run_step("allowlisted", self.command(body), log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, out=out, base_env=base, export_env=("FOO",))
        self.assertEqual(real_env.read_text(encoding="utf-8"), "FOO=1\n")
        self.assertIn("1 environment or output line(s) not on the allowlist were not copied", out.getvalue())
        self.assertNotIn("NODE_OPTIONS", out.getvalue())

    def test_a_private_error_name_prints_as_other(self):
        body = ('class FooBarError(Exception):\n    pass\n'
                'raise FooBarError("secret detail")\n')
        out = io.StringIO()
        run_sealed.run_step("private error", self.command(body), log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, out=out)
        self.assertIn("last error class: other", out.getvalue())
        self.assertNotIn("FooBarError", out.getvalue())
        self.assertNotIn("secret detail", out.getvalue())

    def test_a_standard_error_name_is_printed(self):
        out = io.StringIO()
        run_sealed.run_step("standard error", self.command('raise KeyError("x")'), log_dir=self.logs,
                            recipient="team-key", gpg=self.gpg, out=out)
        self.assertIn("last error class: KeyError", out.getvalue())

    def test_as_user_runs_the_command_as_that_user_with_a_clean_environment(self):
        # The PR's code runs as the unprivileged test user, not the runner: it cannot write the trusted scripts
        # or read this process's environment. The command is wrapped in sudo -u with env -i.
        seen = []

        class FakeChild:
            stdout = io.BytesIO(b"ok\n")

            def wait(self):
                return 0

        def popen(command, stdout, stderr, env):
            seen.append(command)
            return FakeChild()

        run_sealed.run_step("as user", ["python3", "-c", "pass"], log_dir=self.logs, recipient="team-key",
                            gpg=self.gpg, out=io.StringIO(), as_user="pr-tests", popen=popen,
                            base_env=dict(os.environ, PATH="/bin"))
        argv = seen[0]
        self.assertEqual(argv[:6], ["sudo", "-n", "-u", "pr-tests", "--", "env"])
        self.assertIn("-i", argv)
        self.assertEqual(argv[-2:], ["-c", "pass"])

    def test_a_symlinked_junit_report_is_refused_and_its_target_is_not_sealed(self):
        # The junit path is written by untrusted code. A symlink to a runner file must not be followed and sealed.
        # Symlinks are not creatable on every host, so the link is simulated through Path.is_symlink, the check
        # the wrapper makes before it reads anything.
        target = self.dir / "runner-secret.txt"
        target.write_text(SECRET, encoding="utf-8")
        junit = self.dir / "junit.xml"
        junit.write_text("<testsuites/>", encoding="utf-8")
        out = io.StringIO()
        real_is_symlink = Path.is_symlink

        def fake_is_symlink(self_path):
            return self_path == junit or real_is_symlink(self_path)

        with mock.patch.object(Path, "is_symlink", fake_is_symlink):
            code = run_sealed.run_step("symlink", self.command('print("ok")'), log_dir=self.logs,
                                       recipient="team-key", gpg=self.gpg, junit=str(junit), out=out)
        self.assertEqual(code, 1)
        self.assertIn("symbolic link; it was not sealed", out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())
        self.assertFalse((self.logs / "symlink-junit.xml.gpg").exists())
        self.assertEqual(list(self.logs.glob("*staged*")), [])

    def test_main_refuses_a_call_without_a_command_separator(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(run_sealed.main(["--name", "x", "--log-dir", "d", "--recipient", "k"]), 2)
        self.assertIn("give the command after --", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
