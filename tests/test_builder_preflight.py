import importlib.util
from pathlib import Path
import unittest
import sys
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("builder")
pins = load("read_release_pins")
plan = load("plan_suites")
configure = load("configure")
runtime = load("protect_runtime")
sealed = load("run_sealed")


class BuilderTests(unittest.TestCase):
    def test_runner_configuration_permissions_are_removed_before_untrusted_code(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".credentials"
            path.write_text("synthetic-data")
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            runtime.protect_runner_files(temp, run=run)
            self.assertEqual(calls, [["sudo", "-n", "chmod", "go-rwx", str(path)]])
            with self.assertRaises(RuntimeError):
                runtime.protect_runner_files(None, run=run)
    def test_msys_gpg_paths_use_drive_mounts_without_changing_native_gpg_paths(self):
        msys = ("C:/Program Files/Git/usr/bin/gpg.exe",)
        self.assertEqual(sealed.gpg_filename(msys, "D:/a/_temp/log.gpg", windows=True), "/d/a/_temp/log.gpg")
        self.assertEqual(sealed.gpg_filename(msys, "sealed/log.gpg", windows=True), "sealed/log.gpg")
        native = ("C:/Program Files/GnuPG/bin/gpg.exe",)
        self.assertEqual(sealed.gpg_filename(native, "D:/a/_temp/log.gpg", windows=True), "D:/a/_temp/log.gpg")

    def test_runtime_protection_rejects_a_checkout_and_protects_tools_and_parent_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "checkout"
            tools = Path(temp) / "tools" / "bin"
            workspace.mkdir()
            tools.mkdir(parents=True)
            with self.assertRaises(ValueError):
                runtime.targets(str(workspace), (), workspace)
            roots = runtime.targets(str(tools), (), workspace)
            calls = []
            def run(args, **kwargs):
                calls.append(args)
                return subprocess.CompletedProcess(args, 0)
            runtime.protect(roots, run=run)
            self.assertIn(["sudo", "-n", "chmod", "-R", "go-w", str(tools.resolve())], calls)
            self.assertIn(["sudo", "-n", "chmod", "go-w", str(tools.parent.resolve())], calls)

    def test_configuration_streams_secret_values_without_command_line_or_output_exposure(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        configure.set_value("secret", "BUILDER_APP_PRIVATE_KEY", "synthetic-value", run=run)
        self.assertNotIn("synthetic-value", calls[0][0])
        self.assertEqual(calls[0][1]["input"], "synthetic-value")
        self.assertTrue(calls[0][1]["capture_output"])

    def test_dispatches_never_target_private_actions(self):
        for command, kwargs in [("test", {"repository": "aiosxaegra", "sha": "a" * 40}),
                                ("release", {"tag": "v1.2.3"}), ("self-check", {})]:
            args = builder.dispatch_args(command, **kwargs)
            self.assertEqual(args[args.index("--repo") + 1], builder.BUILDER)
            self.assertEqual(args[args.index("--ref") + 1], "main")

    def test_a_branch_is_not_a_release_and_a_moving_ref_is_not_a_test_head(self):
        for tag in ("main", "v1.2", "v1.2.3;echo nope"):
            with self.assertRaises(ValueError):
                builder.dispatch_args("release", tag=tag)
        with self.assertRaises(ValueError):
            builder.dispatch_args("test", repository="aiosxaegra", sha="main")

    def test_missing_test_credentials_are_reported_before_dispatch(self):
        missing = builder.missing_configuration("test", (), ())
        self.assertIn("environment-secret:source-access/BUILDER_APP_PRIVATE_KEY", missing)
        self.assertIn("variable:TEST_LOG_PUBLIC_KEY", missing)

    def test_release_requires_reviewer_and_the_builder_main_branch_not_input_tags(self):
        variables = {"BUILDER_APP_ID", "TEST_LOG_PUBLIC_KEY", "TEST_LOG_RECIPIENT"}
        environment = {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}],
                       "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        good = [{"name": "main", "type": "branch"}]
        env_secrets = builder.RELEASE_SECRETS | {"BUILDER_APP_PRIVATE_KEY"}
        self.assertEqual(builder.missing_configuration("release", variables, (),
                         environment, good, env_secrets), [])
        for wrong in ([{"name": "v*", "type": "tag"}], good + [{"name": "*", "type": "branch"}]):
            self.assertIn("environment:main-only-branch-rule", builder.missing_configuration("release", variables,
                          (), environment, wrong, env_secrets))
        environment["protection_rules"] = []
        self.assertIn("environment:required-reviewer", builder.missing_configuration("release", variables,
                      (), environment, good, env_secrets))

    def test_private_app_key_can_only_be_read_by_approved_source_or_release_jobs(self):
        consumers = []
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            for name, job in yaml.safe_load(path.read_text())["jobs"].items():
                if "BUILDER_APP_PRIVATE_KEY" in str(job):
                    consumers.append((path.name, name))
                    self.assertIn(job.get("environment"), ("source-access", "release-signing"),
                                  f"{path.name}/{name} exposes private-source access without its approval gate")
        self.assertIn(("release.yml", "identity"), consumers)
        self.assertIn(("test.yml", "suite"), consumers)
        self.assertIn(("test.yml", "status"), consumers)
        self.assertIn(("catch-up.yml", "expand"), consumers)
        bridge = (ROOT / ".github/workflows/bridge-deploy.yml").read_text()
        self.assertNotIn("secrets.BUILDER_APP_PRIVATE_KEY", bridge)
        self.assertIn("secrets.BRIDGE_APP_PRIVATE_KEY", bridge)

    def test_preflight_rejects_an_unprotected_repository_key_even_when_environment_key_exists(self):
        missing = builder.missing_configuration("test", (), {"BUILDER_APP_PRIVATE_KEY"},
                                               environment_secrets={"BUILDER_APP_PRIVATE_KEY"})
        self.assertIn("unsafe-repository-secret:BUILDER_APP_PRIVATE_KEY", missing)

    def test_configuration_uploads_the_source_key_only_to_the_two_approved_environments(self):
        calls = []
        def run(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        configure.set_app_key("synthetic-value", run=run)
        self.assertEqual({args[args.index("--env") + 1] for args, _ in calls},
                         {"source-access", "release-signing"})
        self.assertTrue(all(kwargs["input"] == "synthetic-value" for _, kwargs in calls))

    def test_release_pins_come_from_the_selected_release_and_conflicts_fail_closed(self):
        document = {"jobs": {"source": {"steps": [{"env": {"EXPECTED_AEGRA_SHA": "a" * 40,
                                                               "EXPECTED_MEMORY_SHA": "b" * 40}}]}}}
        self.assertEqual(pins.read_pins(document), {"aegra_sha": "a" * 40, "memory_sha": "b" * 40})
        document["other"] = {"EXPECTED_AEGRA_SHA": "c" * 40}
        with self.assertRaises(ValueError):
            pins.read_pins(document)
        for invalid in ({}, {"EXPECTED_AEGRA_SHA": "main", "EXPECTED_MEMORY_SHA": "b" * 40}):
            with self.assertRaises(ValueError):
                pins.read_pins(invalid)

    def test_python_suites_use_the_environment_installed_during_setup_without_resolution(self):
        for repository in ("aiosxaegra", "companion-memory"):
            suite, = plan.SUITES[repository]
            self.assertTrue(suite["run"].startswith(".venv/bin/python -m pytest"))
            self.assertNotIn("uv run", suite["run"])
        memory_setup = plan.SUITES["companion-memory"][0]["setup"]
        self.assertIn("uv sync --project ../aiosxaegra --locked", memory_setup)
        self.assertIn("--editable '.[dev]'", memory_setup)
        self.assertIn("ln -s ../aiosxaegra/.venv .venv", memory_setup)

    def test_desktop_runtime_tests_do_not_skip_the_canonical_native_install(self):
        suites = plan.SUITES["aios-desktop-app"]
        self.assertEqual(suites[0]["setup"], "npm ci --no-audit --no-fund")
        workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
        steps = workflow["jobs"]["suite"]["steps"]
        peer = next(step for step in steps if step.get("with", {}).get("path") == "aiosxaegra")
        self.assertIn("graphs/react_agent/models_registry.json", peer["with"]["sparse-checkout"])
        self.assertFalse(peer["with"]["persist-credentials"])
        native = next(step for step in steps if step.get("name") == "Install the desktop native build headers")
        self.assertEqual(native["if"], "matrix.suite == 'desktop unit tests (vitest)'")
        self.assertIn("libxtst-dev libpng++-dev", native["run"])
        self.assertLess(steps.index(native), next(i for i, step in enumerate(steps) if step.get("name") == "Protect trusted hosted tools from test-user writes"))
        group=workflow["concurrency"]["group"]
        for component in ("github.sha", "inputs.head_sha", "inputs.memory_sha", "inputs.engine_sha", "inputs.diagnostic"):
            self.assertIn(component,group)

    def test_public_self_check_has_no_secrets_private_checkout_or_release(self):
        text = (ROOT / ".github/workflows/self-check.yml").read_text()
        self.assertNotIn("secrets.", text)
        self.assertNotIn("repository:", text)
        self.assertNotIn("release-signing", text)

    def test_release_pin_outputs_exist_and_all_token_actions_support_permission_inputs(self):
        for filename in ("test.yml", "release.yml"):
            text = (ROOT / ".github/workflows" / filename).read_text()
            workflow = yaml.safe_load(text)
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    if step.get("uses", "").startswith("actions/create-github-app-token@"):
                        self.assertEqual(step["uses"], "actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510")
            if filename == "release.yml":
                steps = workflow["jobs"]["source"]["steps"]
                ids = [step.get("id") for step in steps]
                self.assertLess(ids.index("pins"), ids.index("refs"))
                refs = next(step for step in steps if step.get("id") == "refs")
                self.assertTrue(all("steps.pins.outputs." in value for key, value in refs["env"].items() if key != "GH_TOKEN"))


if __name__ == "__main__":
    unittest.main()
