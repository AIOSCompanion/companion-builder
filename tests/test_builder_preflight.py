import importlib.util
from pathlib import Path
import unittest
import sys
import subprocess

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


class BuilderTests(unittest.TestCase):
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
        self.assertIn("secret:BUILDER_APP_PRIVATE_KEY", missing)
        self.assertIn("variable:TEST_LOG_PUBLIC_KEY", missing)

    def test_release_requires_reviewer_and_the_builder_main_branch_not_input_tags(self):
        variables = {"BUILDER_APP_ID", "TEST_LOG_PUBLIC_KEY", "TEST_LOG_RECIPIENT"}
        environment = {"protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}]}],
                       "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        good = [{"name": "main", "type": "branch"}]
        self.assertEqual(builder.missing_configuration("release", variables, {"BUILDER_APP_PRIVATE_KEY"},
                         environment, good, builder.RELEASE_SECRETS), [])
        for wrong in ([{"name": "v*", "type": "tag"}], good + [{"name": "*", "type": "branch"}]):
            self.assertIn("environment:main-only-branch-rule", builder.missing_configuration("release", variables,
                          {"BUILDER_APP_PRIVATE_KEY"}, environment, wrong, builder.RELEASE_SECRETS))
        environment["protection_rules"] = []
        self.assertIn("environment:required-reviewer", builder.missing_configuration("release", variables,
                      {"BUILDER_APP_PRIVATE_KEY"}, environment, good, builder.RELEASE_SECRETS))

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
        self.assertIn("--extra dev", plan.SUITES["companion-memory"][0]["setup"])

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
