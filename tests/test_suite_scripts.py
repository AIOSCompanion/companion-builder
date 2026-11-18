"""The PR test workflow's scripts: the fixed suite matrix, the isolation proof, the suite runner and the status.

The isolation proof is the claim the no-egress rule rests on, so each way it could pass wrongly is pinned here:
a command that never ran, an extra network interface, a curl failure that is not a connect or resolve failure,
and a curl that succeeded. The suite runner's return code is pinned too: a failing suite must fail the job.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plan_suites = load("plan_suites")
egress_probe = load("egress_probe")
run_suite = load("run_suite")
post_status = load("post_status")
prepare_untrusted = load("prepare_untrusted")
SHA = "a" * 40
LO_ONLY = "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536\n"


class PlanTests(unittest.TestCase):
    def test_each_repository_gets_its_fixed_suites(self):
        self.assertEqual(len(plan_suites.SUITES['aiosxaegra']), 8)
        self.assertTrue(all('tests/unit' in suite['run'] for suite in plan_suites.SUITES['aiosxaegra']))
        self.assertEqual(plan_suites.matrix_for("aios-desktop-app").count('"suite"'), 2)

    def test_python_suites_install_uv_for_the_test_user_only(self):
        self.assertIn("pip install --quiet --user uv", plan_suites.matrix_for("companion-memory"))

    def test_an_unknown_repository_is_refused(self):
        with self.assertRaisesRegex(plan_suites.PlanError, "unknown repository"):
            plan_suites.matrix_for("some-other-repo")

    def test_go_tests_run_offline_and_compile_the_renderer_when_present(self):
        suite, = plan_suites.SUITES["companion-engine"]
        self.assertIn("go mod download", suite["setup"])
        self.assertIn("GOPROXY=off go test -mod=readonly", suite["run"])
        self.assertIn("npm --prefix ui run check:protocol && npm --prefix ui run build", suite["run"])
        status = post_status.gh_args("companion-engine", SHA, "success", "https://example.test/run")
        self.assertIn(f"repos/AIOSCompanion/companion-engine/statuses/{SHA}", status)

    def test_the_matrix_is_written_to_github_output(self):
        with tempfile.TemporaryDirectory(prefix="plan ") as temp:
            out = Path(temp) / "output"
            code = plan_suites.main(["--repository", "companion-memory"], env={"GITHUB_OUTPUT": str(out)})
            self.assertEqual(code, 0)
            self.assertTrue(out.read_text().startswith("matrix=[{"))


class ProbeTests(unittest.TestCase):
    def fake(self, *, outside=0, inside_stdout="", inside_code=0):
        calls = []

        def run(argv, capture_output, check, text=False):
            calls.append(argv)
            if "curl" in argv and argv[0] == "sudo" and "unshare" not in argv:
                return subprocess.CompletedProcess(argv, outside)
            return subprocess.CompletedProcess(argv, inside_code, stdout=inside_stdout.encode())

        return run, calls

    def inside(self, rc, links=LO_ONLY, sentinel=True):
        text = ("PROBE_RAN\n" if sentinel else "") + links + f"CURL_RC={rc}\n"
        return text

    def test_only_loopback_and_a_connect_or_resolve_failure_pass(self):
        for rc in (6, 7):
            with self.subTest(rc=rc):
                run, calls = self.fake(inside_stdout=self.inside(rc))
                message = egress_probe.probe(run=run)
                self.assertIn("only loopback exists", message)
                self.assertTrue(any("unshare" in call for call in calls))

    def test_a_successful_request_inside_fails(self):
        run, _ = self.fake(inside_stdout=self.inside(0))
        with self.assertRaisesRegex(egress_probe.ProbeError, "succeeded inside"):
            egress_probe.probe(run=run)

    def test_a_timeout_is_not_a_proof_of_isolation(self):
        run, _ = self.fake(inside_stdout=self.inside(28))
        with self.assertRaisesRegex(egress_probe.ProbeError, "exited 28"):
            egress_probe.probe(run=run)

    def test_curl_missing_is_not_a_proof_of_isolation(self):
        run, _ = self.fake(inside_stdout=self.inside(127))
        with self.assertRaisesRegex(egress_probe.ProbeError, "exited 127"):
            egress_probe.probe(run=run)

    def test_an_extra_interface_fails_the_probe(self):
        links = LO_ONLY + "2: eth0@if7: <BROADCAST,UP> mtu 1500\n"
        run, _ = self.fake(inside_stdout=self.inside(6, links=links))
        with self.assertRaisesRegex(egress_probe.ProbeError, "other than loopback"):
            egress_probe.probe(run=run)

    def test_a_probe_that_never_ran_inside_fails(self):
        run, _ = self.fake(inside_stdout=self.inside(6, sentinel=False))
        with self.assertRaisesRegex(egress_probe.ProbeError, "did not run inside"):
            egress_probe.probe(run=run)

    def test_an_unreachable_calibration_outside_is_not_a_proof(self):
        run, _ = self.fake(outside=6, inside_stdout=self.inside(6))
        with self.assertRaisesRegex(egress_probe.ProbeError, "calibration failed"):
            egress_probe.probe(run=run)


class SuiteRunnerTests(unittest.TestCase):
    def env(self, **extra):
        base = {"SUITE_NAME": "engine unit tests (pytest)", "TEST_COMMAND": "python -m pytest",
                "LOG_RECIPIENT": "team-key", "SEALED_LOG_DIR": "/runner/temp/sealed/logs",
                "PATH": "/bin"}
        base.update(extra)
        return base

    def test_the_suite_runs_through_the_wrapper_as_the_test_user_in_a_namespace(self):
        seen = []

        def run(argv, env, check):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        self.assertEqual(run_suite.main(env=self.env(), run=run), 0)
        argv = seen[0]
        self.assertIn("run_sealed.py", argv[3])
        self.assertIn("--log-dir", argv)
        self.assertIn("/runner/temp/sealed/logs", argv)
        self.assertIn("sudo", argv)
        self.assertIn("unshare", argv)
        self.assertIn("--net", argv)
        self.assertIn("/usr/bin/timeout", argv)
        self.assertIn("3300s", argv)
        inner = argv[-1]
        self.assertIn("runuser -u pr-tests", inner)
        self.assertIn("env -i", inner)
        self.assertIn("HOME=/home/pr-tests", inner)
        self.assertNotIn("sudo -E", " ".join(argv))

    def test_the_suite_command_is_quoted_not_expanded_by_the_shell(self):
        inner = run_suite.inner_command("echo $HOME; rm -rf /", "pr-tests", "/bin")
        self.assertIn("bash -c 'echo $HOME; rm -rf /'", inner)

    def test_a_failing_suite_returns_its_failure(self):
        def run(argv, env, check):
            return subprocess.CompletedProcess(argv, 3)

        self.assertEqual(run_suite.main(env=self.env(), run=run), 3)

    def test_a_missing_input_refuses_before_running(self):
        ran = []

        def run(argv, env, check):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        with contextlib.redirect_stdout(io.StringIO()):
            code = run_suite.main(env=self.env(LOG_RECIPIENT=""), run=run)
        self.assertEqual(code, 2)
        self.assertEqual(ran, [])

    def test_invalid_deadlines_refuse_before_running(self):
        for value in ("0", "-1", "3301", "nonsense"):
            ran=[]
            with contextlib.redirect_stdout(io.StringIO()):
                code=run_suite.main(env=self.env(SUITE_TIMEOUT_SECONDS=value),
                                    run=lambda *a, **kw: ran.append(a))
            self.assertEqual(code,2)
            self.assertEqual(ran,[])

    def test_timeout_stays_inside_sealer_and_outside_privileged_namespace(self):
        args=run_suite.build_argv("python", "run_sealed.py", "fixture", "logs", "recipient", "sleep 9", "/bin", timeout_seconds=600)
        self.assertLess(args.index("run_sealed.py"),args.index("/usr/bin/timeout"))
        self.assertLess(args.index("sudo"),args.index("/usr/bin/timeout"))
        self.assertLess(args.index("/usr/bin/timeout"),args.index("unshare"))
        self.assertIn("600s",args)


class PrepareTests(unittest.TestCase):
    def test_a_missing_user_is_created_and_the_checkout_handed_over(self):
        calls = []

        def run(argv, capture_output=False, check=True):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 1 if argv[0] == "id" else 0)

        self.assertEqual(prepare_untrusted.prepare("pr-tests", "pr", run=run), "created; home not requested")
        self.assertIn("useradd", calls[1])
        self.assertEqual(calls[2], ["sudo", "-n", "chown", "-R", "pr-tests:pr-tests", "pr"])

    def test_the_home_is_locked_to_711_when_the_image_allows(self):
        calls = []

        def run(argv, capture_output=False, check=True):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0)

        state = prepare_untrusted.prepare("pr-tests", "pr", run=run, home="/home/runner")
        self.assertIn("home locked 711", state)
        self.assertIn(["sudo", "-n", "chmod", "711", "/home/runner"], calls)

    def test_a_refused_home_lock_is_reported_and_is_not_fatal(self):
        def run(argv, capture_output=False, check=True):
            return subprocess.CompletedProcess(argv, 1 if "chmod" in argv else 0)

        state = prepare_untrusted.prepare("pr-tests", "pr", run=run, home="/home/runner")
        self.assertIn("could not lock", state)

    def test_a_failed_handover_is_an_error(self):
        def run(argv, capture_output=False, check=True):
            return subprocess.CompletedProcess(argv, 1 if "chown" in argv else 0)

        with self.assertRaisesRegex(RuntimeError, "could not hand the PR checkout"):
            prepare_untrusted.prepare("pr-tests", "pr", run=run)


class StatusTests(unittest.TestCase):
    def test_success_and_failure_map_to_the_status_state(self):
        self.assertEqual(post_status.state_for("success"), "success")
        self.assertEqual(post_status.state_for("failure"), "failure")
        self.assertEqual(post_status.state_for("cancelled"), "failure")

    def test_the_description_carries_no_output(self):
        args = post_status.gh_args("aiosxaegra", SHA, "success", "https://example.invalid/run/1")
        description = [a for a in args if a.startswith("description=")][0]
        self.assertEqual(description, "description=Tests success; logs are sealed, see the run")

    def test_an_unknown_repository_or_a_short_sha_is_refused(self):
        with self.assertRaisesRegex(post_status.StatusError, "unknown repository"):
            post_status.gh_args("other", SHA, "success", "u")
        with self.assertRaisesRegex(post_status.StatusError, "40 lowercase hex"):
            post_status.gh_args("aiosxaegra", "abc", "success", "u")

    def test_a_failed_post_exits_nonzero(self):
        def run(argv, env, check, stdout, stderr):
            return subprocess.CompletedProcess(argv, 1)

        env = {"REPOSITORY": "aiosxaegra", "SHA": SHA, "NEEDS_RESULT": "success", "RUN_URL": "u", "GH_TOKEN": "x"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(post_status.main(env=env, run=run), 1)
        self.assertEqual(stderr.getvalue().strip(), "post_status: the status could not be posted")


if __name__ == "__main__":
    unittest.main()
