"""The permission probe. Verdicts come from errno, taken inside the test user's own Python process.

Gated (a success is a breach, a missing target is a refusal to certify): the GITHUB_* files, the snapshot, RUNNER_TEMP,
the PATH entries and their parents, the interpreter's stdlib and site-packages, the checkout's credential files.
Reported (never gated): the runner install directory's files and the worker's environment, each as absent, denied
or READABLE. READABLE prints a warning. Discovery reads each process's executable link, not its command line.
"""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("permission_probe", ROOT / "scripts" / "permission_probe.py")
permission_probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(permission_probe)


def make_run(*, allowed=(), denial="EACCES", runner_ok=True, identity="pr-tests", positive_ok=True):
    """A runner that executes the probe's operations. The test user is refused unless a target is allowed."""
    def run(argv, capture_output=False, text=False, check=False):
        if permission_probe.PROBE_SCRIPT in argv:
            ops = json.loads(argv[-1])
            as_user = argv[0] == "sudo"
            lines = [f"user={identity if as_user else 'runner'}"]
            for label, kind, a, b in ops:
                if as_user:
                    allowed_here = a in allowed or b in allowed or (positive_ok and label.startswith("positive"))
                    code = "OK" if allowed_here else denial
                else:
                    code = "OK" if runner_ok else "EACCES"
                lines.append(f"{label}\t{code}")
            return subprocess.CompletedProcess(argv, 0, stdout="\n".join(lines) + "\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    return run


class PermissionProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="permission probe ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github_env = str(self.root / "github-env")
        self.github_output = str(self.root / "github-output")
        self.github_path = str(self.root / "github-path")
        for name in (self.github_env, self.github_output, self.github_path):
            Path(name).write_text("", encoding="utf-8")
        self.runner_temp = self.root / "runner-temp"
        self.runner_temp.mkdir()
        self.trusted = self.runner_temp / "trusted"
        self.trusted.mkdir()
        (self.trusted / "MANIFEST.sha256").write_text("", encoding="utf-8")  # every real snapshot has one
        self.workdir = self.root / "pr"
        self.workdir.mkdir()
        self.path_dir = self.root / "bin"
        self.path_dir.mkdir()
        self.runner_dir = self.root / "runner"
        self.runner_dir.mkdir()
        for name in permission_probe.RUNNER_REPORT_FILES:
            (self.runner_dir / name).write_text("secret", encoding="utf-8")
        self.interp = self.root / "interp"
        self.interp.mkdir()
        self.home = str(self.root / "home-runner")  # the probe checks the home exists as the runner
        Path(self.home).mkdir()
        self.procfs = self.root / "procfs"
        (self.procfs / "4242").mkdir(parents=True)
        (self.procfs / "4242" / "environ").write_bytes(b"RUNNER_TOKEN=fake\0")

    def probe(self, run, path_dirs=None, interpreter_paths=(), workspace="", worker_pid=4242, runner_root=None):
        return permission_probe.probe(
            user="pr-tests", github_env=self.github_env, github_output=self.github_output,
            github_path=self.github_path, trusted=str(self.trusted), runner_temp=str(self.runner_temp),
            workdir=str(self.workdir), workspace=workspace,
            path_dirs=[str(self.path_dir)] if path_dirs is None else path_dirs, home=self.home,
            runner_root=str(self.runner_dir) if runner_root is None else runner_root,
            worker_pid=worker_pid, interpreter="/usr/bin/python3", interpreter_paths=interpreter_paths,
            proc=str(self.procfs), run=run)

    def test_every_gate_is_refused_and_the_report_is_shown(self):
        lines = self.probe(make_run())
        self.assertTrue(any(line.startswith("interpreter ") and "ran as pr-tests" in line for line in lines))
        for needle in ("write GITHUB_ENV", "write GITHUB_OUTPUT", "write GITHUB_PATH", "rename the snapshot",
                       "create RUNNER_TEMP/sealed"):
            self.assertTrue(any(needle in line and "refused" in line for line in lines), needle)
        self.assertTrue(any("runner file .credentials: denied" in line for line in lines), lines)
        self.assertTrue(any("worker environment" in line and "denied" in line for line in lines), lines)
        self.assertFalse(any("WARNING" in line for line in lines), lines)

    def test_a_write_to_github_env_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "could write GITHUB_ENV"):
            self.probe(make_run(allowed=[self.github_env]))

    def test_a_write_to_github_output_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "could write GITHUB_OUTPUT"):
            self.probe(make_run(allowed=[self.github_output]))

    def test_a_write_to_github_path_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "could write GITHUB_PATH"):
            self.probe(make_run(allowed=[self.github_path]))

    def test_creating_the_sealed_directory_in_runner_temp_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "could create RUNNER_TEMP/sealed"):
            self.probe(make_run(allowed=[str(self.runner_temp / "sealed")]))

    def test_existing_install_logs_are_preserved_and_probe_targets_do_not_already_exist(self):
        sealed = self.runner_temp / "sealed"
        sealed.mkdir()
        log = sealed / "install.log.gpg"
        log.write_bytes(b"synthetic-encrypted-log")
        calls = []
        fake = make_run()
        def run(argv, **kwargs):
            if permission_probe.PROBE_SCRIPT in argv:
                calls.extend(json.loads(argv[-1]))
            return fake(argv, **kwargs)
        lines = self.probe(run)
        for label, kind, target, _ in calls:
            if kind in ("create", "mkdir") and "sealed" in label:
                self.assertFalse(Path(target).exists(), "an existing target gives EEXIST rather than a permission result")
        self.assertTrue(any("rename RUNNER_TEMP/sealed: refused" in line for line in lines))
        self.assertEqual(log.read_bytes(), b"synthetic-encrypted-log")

    def test_existing_sealed_directory_still_refuses_file_creation_and_replacement(self):
        sealed = self.runner_temp / "sealed"
        sealed.mkdir()
        for path in (sealed / "probe-new", sealed / "probe-new-dir", sealed):
            with self.subTest(path=path), self.assertRaises(permission_probe.ProbeError):
                self.probe(make_run(allowed=[str(path)]))

    def test_a_rename_of_the_snapshot_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "could rename the snapshot"):
            self.probe(make_run(allowed=[str(self.trusted)]))

    def test_a_write_into_a_path_directory_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "trusted PATH directory"):
            self.probe(make_run(allowed=[str(self.path_dir / "probe-new")]))

    def test_a_write_to_the_parent_of_a_path_directory_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "parent of trusted PATH directory"):
            self.probe(make_run(allowed=[str(self.root / "probe-new")]))

    def test_a_write_in_the_nearest_ancestor_of_a_missing_path_entry_is_refused(self):
        missing = str(self.root / "missing" / "bin")
        with self.assertRaisesRegex(permission_probe.ProbeError, "nearest ancestor of missing PATH entry"):
            self.probe(make_run(allowed=[str(self.root / "probe-new")]), path_dirs=[missing])

    def test_the_workspace_root_is_skipped_as_a_parent(self):
        lines = self.probe(make_run(), path_dirs=[str(self.path_dir)], workspace=str(self.root))
        self.assertFalse(any("parent of trusted PATH directory" in line for line in lines))

    def test_a_write_to_the_interpreter_path_is_refused(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "trusted interpreter path"):
            self.probe(make_run(allowed=[str(self.interp / "probe-new")]), interpreter_paths=[str(self.interp)])

    def test_a_readable_checkout_credential_file_is_refused(self):
        credential = self.runner_temp / "git-credentials-abc"
        credential.write_text("token", encoding="utf-8")
        with self.assertRaisesRegex(permission_probe.ProbeError, "credential file git-credentials-abc"):
            self.probe(make_run(allowed=[str(credential)]))
        credential.unlink()

    def test_a_readable_runner_file_is_a_warning_not_a_refusal(self):
        target = f"{self.runner_dir}/.credentials_rsaparams"
        lines = self.probe(make_run(allowed=[target]))
        self.assertTrue(any("WARNING" in line and "READABLE" in line and ".credentials_rsaparams" in line
                            for line in lines), lines)

    def test_a_readable_worker_environment_is_a_warning_not_a_refusal(self):
        lines = self.probe(make_run(allowed=[f"{self.procfs}/4242/environ"]))
        self.assertTrue(any("WARNING" in line and "READABLE" in line and "environ" in line for line in lines), lines)

    def test_an_absent_runner_file_is_reported_absent(self):
        (self.runner_dir / ".runner").unlink()
        lines = self.probe(make_run())
        self.assertTrue(any(".runner: absent" in line for line in lines), lines)

    def test_no_worker_or_runner_process_is_a_warning_not_a_refusal(self):
        lines = permission_probe.probe(
            user="pr-tests", github_env=self.github_env, github_output=self.github_output,
            github_path=self.github_path, trusted=str(self.trusted), runner_temp=str(self.runner_temp),
            workdir=str(self.workdir), workspace="", path_dirs=[str(self.path_dir)], home=self.home,
            runner_root=None, worker_pid=None, interpreter="/usr/bin/python3", interpreter_paths=(),
            proc=str(self.procfs), run=make_run())
        self.assertTrue(any("no Runner.Listener process" in line for line in lines), lines)
        self.assertTrue(any("no Runner.Worker process" in line for line in lines), lines)

    def test_a_failed_positive_control_means_a_denial_proves_nothing(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "positive control failed"):
            self.probe(make_run(positive_ok=False))

    def test_a_failed_runner_control_refuses_the_probe(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "control failed"):
            self.probe(make_run(runner_ok=False))

    def test_the_wrong_identity_refuses_the_probe(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "did not run as the test user"):
            self.probe(make_run(identity="runner"))

    def test_an_errno_that_is_not_a_permission_denial_refuses_the_probe(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "not a permission denial"):
            self.probe(make_run(denial="ENOENT"))

    def test_a_wrong_snapshot_path_fails_with_the_path_named_not_as_a_pass(self):
        wrong = str(self.root / "no-such-snapshot")
        with self.assertRaisesRegex(permission_probe.ProbeError, "does not exist as the runner"):
            permission_probe.probe(user="pr-tests", github_env=self.github_env, github_output=self.github_output,
                                   github_path=self.github_path, trusted=wrong, runner_temp=str(self.runner_temp),
                                   workdir=str(self.workdir), workspace="", path_dirs=[str(self.path_dir)],
                                   home=self.home, runner_root=str(self.runner_dir), worker_pid=4242,
                                   interpreter="/usr/bin/python3", interpreter_paths=(), proc=str(self.procfs),
                                   run=make_run())

    def test_the_users_own_bin_is_not_checked(self):
        lines = self.probe(make_run(), path_dirs=["/home/pr-tests/.local/bin"])
        self.assertFalse(any("trusted PATH directory" in line for line in lines))

    def test_a_readable_home_is_reported_not_refused(self):
        lines = self.probe(make_run(allowed=[self.home]))
        self.assertTrue(any("readable" in line and "711" in line for line in lines), lines)

    def test_discovery_matches_the_executable_basename_and_takes_the_grandparent(self):
        proc = self.root / "proc"
        (proc / "777").mkdir(parents=True)
        (proc / "888").mkdir(parents=True)
        exes = {str(proc / "777" / "exe"): "/home/runner/runner/bin/Runner.Listener",
                str(proc / "888" / "exe"): "/home/runner/runner/bin/Runner.Worker"}

        def readlink(path):
            return exes[path]

        root = permission_probe.discover_runner_root(str(proc), readlink=readlink)
        self.assertEqual(root, "/home/runner/runner")  # parent.parent of /home/runner/runner/bin/Runner.Listener
        self.assertNotEqual(root, "/home/runner/runner/bin")  # the executable's parent is not the root
        self.assertEqual(permission_probe.discover_worker_pid(str(proc), readlink=readlink), 888)

    def test_discovery_does_not_match_a_command_line_that_only_mentions_the_name(self):
        proc = self.root / "proc"
        (proc / "900").mkdir(parents=True)
        exes = {str(proc / "900" / "exe"): "/usr/bin/grep"}

        def readlink(path):
            return exes[path]

        self.assertIsNone(permission_probe.discover_runner_root(str(proc), readlink=readlink))

    def test_missing_processes_are_none_not_an_error(self):
        empty = self.root / "empty-proc"
        empty.mkdir()
        self.assertIsNone(permission_probe.discover_runner_root(str(empty), readlink=lambda p: ""))
        self.assertIsNone(permission_probe.discover_worker_pid(str(empty), readlink=lambda p: ""))

    def test_missing_inputs_refuse_before_any_check(self):
        with self.assertRaisesRegex(permission_probe.ProbeError, "must be given"):
            permission_probe.probe(user="pr-tests", github_env="", github_output=self.github_output,
                                   github_path=self.github_path, trusted=str(self.trusted),
                                   runner_temp=str(self.runner_temp), workdir=str(self.workdir), workspace="",
                                   path_dirs=[], home=self.home, interpreter="/usr/bin/python3", run=make_run())

    def test_test_yml_probe_call_parses_against_the_probe_parser(self):
        # A review found the probe's call missing --workdir, so argparse exited before the suite ran. This parses the
        # workflow's own call, token by token, against the probe's parser, so a drifted call fails the test.
        import shlex
        workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        marker = "permission_probe.py\""
        index = workflow.index(marker)
        line = workflow[index + len(marker):].splitlines()[0]
        args = permission_probe.build_parser().parse_args(shlex.split(line))
        self.assertEqual(args.workdir, "pr")
        self.assertEqual(args.trusted, "$RUNNER_TEMP/trusted")
        self.assertEqual(args.home, "$HOME")
        self.assertEqual(args.path, "$PATH")

    def test_the_command_prints_the_lines_and_the_verdict(self):
        # The runner install root is the grandparent of the listener's executable; the worker's environ is read from it.
        proc = self.root / "proc"
        (proc / "999").mkdir(parents=True)
        (proc / "1000").mkdir(parents=True)
        (proc / "1000" / "environ").write_bytes(b"RUNNER_TOKEN=fake\0")
        listener = f"{self.runner_dir.as_posix()}/bin/Runner.Listener"
        worker = f"{self.runner_dir.as_posix()}/bin/Runner.Worker"
        exes = {str(proc / "999" / "exe"): listener, str(proc / "1000" / "exe"): worker}
        env = {"GITHUB_ENV": self.github_env, "GITHUB_OUTPUT": self.github_output,
               "GITHUB_PATH": self.github_path, "RUNNER_TEMP": str(self.runner_temp)}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = permission_probe.main(["--trusted", str(self.trusted), "--workdir", str(self.workdir),
                                          "--path", str(self.path_dir), "--home", self.home],
                                         run=make_run(), env=env, proc=str(proc), readlink=exes.__getitem__)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("calibrated", out.getvalue())
        self.assertIn("runner file .runner: denied", out.getvalue())
        # Three runner files and the worker environ are reported; nothing is a warning on a clean run.
        self.assertIn("4 runner item(s) reported, 0 warning(s)", out.getvalue())

    def test_the_final_line_counts_a_readable_item_as_a_warning(self):
        proc = self.root / "proc"
        (proc / "999").mkdir(parents=True)
        (proc / "1000").mkdir(parents=True)
        (proc / "1000" / "environ").write_bytes(b"RUNNER_TOKEN=fake\0")
        listener = f"{self.runner_dir.as_posix()}/bin/Runner.Listener"
        worker = f"{self.runner_dir.as_posix()}/bin/Runner.Worker"
        exes = {str(proc / "999" / "exe"): listener, str(proc / "1000" / "exe"): worker}
        env = {"GITHUB_ENV": self.github_env, "GITHUB_OUTPUT": self.github_output,
               "GITHUB_PATH": self.github_path, "RUNNER_TEMP": str(self.runner_temp)}
        out = io.StringIO()
        target = f"{self.runner_dir.as_posix()}/.runner"  # the probe's own string form
        with contextlib.redirect_stdout(out):
            code = permission_probe.main(["--trusted", str(self.trusted), "--workdir", str(self.workdir),
                                          "--path", str(self.path_dir), "--home", self.home],
                                         run=make_run(allowed=[target]), env=env, proc=str(proc),
                                         readlink=exes.__getitem__)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("READABLE", out.getvalue())
        self.assertIn("warning(s)", out.getvalue())
        self.assertNotIn("0 warning(s)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
