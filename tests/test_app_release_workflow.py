import copy
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release-app.yml").read_text())
BUILDS = {"build-windows": ("windows-2022", "windows-amd64"), "build-macos": ("macos-15", "darwin-arm64")}
APPLE_SECRETS = ("CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER")
PUBLIC_READ_STEP = "Bind only the expected downloadable assets to this source commit"
PUBLIC_READ_ENV = {"APP_ASSET_DIR", "APP_RELEASE_REPOSITORY", "PUBLIC_READ_TOKEN"}
GITHUB_TOKEN_REF = "${{ github.token }}"


def leaves(node, path=()):
    """Every scalar in the parsed workflow, with the path of keys and indexes that holds it."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from leaves(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from leaves(value, path + (index,))
    else:
        yield path, str(node)


def github_token_locations(workflow):
    """The path of every key or value that names the job's GITHUB_TOKEN."""
    return [path for path, value in leaves(workflow)
            if re.search(r"github\.token|GITHUB_TOKEN", " ".join(map(str, path)) + " " + value, re.IGNORECASE)]


def public_read_step(workflow):
    steps = workflow["jobs"]["build-windows"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == PUBLIC_READ_STEP)
    return index, steps[index]


def github_token_violations(workflow):
    """The placement rules for the job's GITHUB_TOKEN. An empty list means the workflow obeys them."""
    violations = []
    if workflow.get("permissions") != {"contents": "read"}:
        violations.append("top-level permissions are not contents: read")
    for name, job in workflow["jobs"].items():
        if "permissions" in job:
            violations.append(f"{name} sets its own permissions, which can widen the token")
    index, step = public_read_step(workflow)
    expected = [("jobs", "build-windows", "steps", index, "env", "PUBLIC_READ_TOKEN")]
    found = github_token_locations(workflow)
    if found != expected:
        violations.append(f"GITHUB_TOKEN appears at {found}, not only at {expected}")
    if set(step) != {"name", "env", "run"} or "stage --platform windows-amd64" not in step["run"]:
        violations.append("the public-read step carries inputs beyond its env and staging command")
    if set(step.get("env", {})) != PUBLIC_READ_ENV or step["env"]["PUBLIC_READ_TOKEN"] != GITHUB_TOKEN_REF:
        violations.append("the public-read step's env is not exactly the public-read inputs")
    return violations


class AppWorkflowTests(unittest.TestCase):
    def test_only_owner_dispatch_from_public_builder_main_enters_approved_jobs(self):
        trigger = WORKFLOW.get("on", WORKFLOW.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertFalse(trigger["workflow_dispatch"]["inputs"]["allow_unsigned_macos_test"]["default"])
        self.assertIn("github.ref == 'refs/heads/main'", WORKFLOW["jobs"]["identity"]["if"])
        self.assertIn("github.repository == 'AIOSCompanion/companion-builder'", WORKFLOW["jobs"]["identity"]["if"])
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        self.assertEqual(set(WORKFLOW["jobs"]), {"identity", "build-windows", "build-macos", "finalize"})
        for job in WORKFLOW["jobs"].values():
            self.assertEqual(job["environment"], "release-signing")

    def test_publisher_and_signing_credentials_never_reach_build_or_dependency_install(self):
        for name, job in WORKFLOW["jobs"].items():
            self.assertNotIn("PUBLISH_RELEASES_TOKEN", str(job.get("env", {})))
            self.assertNotIn("CSC_KEY_PASSWORD", str(job.get("env", {})))
            for step in job["steps"]:
                env = str(step.get("env", {}))
                if "PUBLISH_RELEASES_TOKEN" in env:
                    self.assertTrue(any(value in step["name"] for value in ("draft", "Publish", "Finalize")))
                    self.assertIn("run_sealed.py", step["run"])
                if any(secret in env for secret in APPLE_SECRETS):
                    self.assertEqual(name, "build-macos")
                    self.assertEqual(step["name"], "Sign notarize and staple the macOS arm64 bundle")
                    self.assertIn("!inputs.allow_unsigned_macos_test", step["if"])
                if "BUILDER_APP_PRIVATE_KEY" in str(step):
                    expected_repo = "aios-desktop-app" if step.get("id") == "resource-token" else "companion-engine"
                    self.assertEqual(step["with"]["repositories"], expected_repo)
                    self.assertEqual(step["with"]["permission-contents"], "read")

    def test_the_windows_job_never_references_the_apple_signing_secrets(self):
        # Merge gate F2 (17 Nov 2026): one matrix job meant the Windows runner, which runs npm install scripts and
        # go run, was sent the Apple secrets the macOS leg's signing step references.
        windows = str(WORKFLOW["jobs"]["build-windows"])
        for secret in APPLE_SECRETS:
            self.assertNotIn(secret, windows)
        self.assertNotIn("strategy", WORKFLOW["jobs"]["build-windows"])
        self.assertNotIn("strategy", WORKFLOW["jobs"]["build-macos"])

    def test_the_built_mac_app_runs_only_in_a_step_without_signing_material(self):
        # Merge gate F1 (17 Nov 2026): the signing step executed the built app's `Companion version`.
        steps = WORKFLOW["jobs"]["build-macos"]["steps"]
        names = [step.get("name") or step.get("uses") for step in steps]
        check = steps[names.index("Check the macOS bundle version without credentials")]
        sign = steps[names.index("Sign notarize and staple the macOS arm64 bundle")]
        self.assertTrue(check["run"].endswith("app_release.py\" check-macos"))
        self.assertNotIn("env", check)
        self.assertNotIn("if", check)
        self.assertEqual(set(sign["env"]), set(APPLE_SECRETS))
        self.assertLess(names.index("Build without publisher or signing credentials"), steps.index(check))
        self.assertLess(steps.index(check), steps.index(sign))
        for step in steps[steps.index(sign) + 1:]:
            self.assertFalse(any(secret in str(step.get("env", {})) for secret in APPLE_SECRETS))

    def test_private_git_output_is_sealed_and_only_hash_pins_drive_checkout(self):
        for job in BUILDS:
            steps = WORKFLOW["jobs"][job]["steps"]
            private = next(step for step in steps if step.get("name") == "Check out private source at the validated immutable commit")
            self.assertIn("run_sealed.py", private["run"])
            self.assertIn("app_release.py", private["run"])
            self.assertTrue(private["run"].endswith("checkout"))
            resources = next(step for step in steps if step.get("name") == "Check out the pinned bundled MCP source")
            self.assertTrue(resources["run"].endswith("checkout-resources"))
            self.assertEqual(resources["env"], {"GH_TOKEN": "${{ steps.resource-token.outputs.token }}"})
            stage = next(step for step in steps if step.get("name", "").startswith("Build and stage the native"))
            self.assertNotIn("env", stage)
            self.assertLess(steps.index(resources), steps.index(stage))
            env = WORKFLOW["jobs"][job]["env"]
            self.assertEqual(env["SOURCE_SHA"], "${{ needs.identity.outputs.commit_sha }}")
            self.assertEqual(env["TAG_OBJECT_SHA"], "${{ needs.identity.outputs.tag_object_sha }}")

    def test_only_sealed_logs_are_uploaded_as_actions_artifacts(self):
        for job in WORKFLOW["jobs"].values():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/upload-artifact@"):
                    self.assertTrue(step["with"]["path"].endswith("/*.gpg"))
                    self.assertEqual(step["with"]["retention-days"], 1)

    def test_both_required_native_rows_finish_before_final_draft_verification(self):
        for job, (runner, platform) in BUILDS.items():
            self.assertEqual(WORKFLOW["jobs"][job]["runs-on"], runner)
            self.assertEqual(WORKFLOW["jobs"][job]["needs"], "identity")
            for step in WORKFLOW["jobs"][job]["steps"]:
                if "--platform" in step.get("run", ""):
                    self.assertTrue(step["run"].endswith(f"--platform {platform}"))
        self.assertEqual(WORKFLOW["jobs"]["finalize"]["needs"], ["identity", "build-windows", "build-macos"])
        self.assertNotIn("self-hosted", str(WORKFLOW))

    def test_go_is_pinned_by_the_sources_go_mod_and_node_comes_from_the_uis_engines(self):
        for job in BUILDS:
            steps = WORKFLOW["jobs"][job]["steps"]
            names = [step.get("name") or step.get("uses") for step in steps]
            resolved = next(step for step in steps if step.get("id") == "toolchain")
            self.assertIn("--export-output go_version --export-output node_version", resolved["run"])
            self.assertTrue(resolved["run"].endswith("app_release.py\" toolchain"))
            go = next(step for step in steps if step.get("uses") == "actions/setup-go@v5")
            node = next(step for step in steps if step.get("uses") == "actions/setup-node@v4")
            self.assertEqual(go["with"], {"go-version": "${{ steps.toolchain.outputs.go_version }}", "cache": False})
            self.assertEqual(node["with"], {"node-version": "${{ steps.toolchain.outputs.node_version }}"})
            checkout = names.index("Check out private source at the validated immutable commit")
            preflight = names.index("Recheck the checkout, exact tag and toolchain before building")
            self.assertLess(checkout, steps.index(resolved))
            self.assertLess(steps.index(resolved), steps.index(go))
            self.assertLess(steps.index(node), preflight)
            self.assertLess(preflight, names.index("Build without publisher or signing credentials"))
            self.assertEqual(WORKFLOW["jobs"][job]["env"]["GOTOOLCHAIN"], "local")

    def test_one_builder_variable_names_the_destination_and_todays_repositories_never_appear(self):
        text = (ROOT / ".github/workflows/release-app.yml").read_text()
        self.assertEqual(text.count("vars.APP_RELEASE_REPOSITORY"), 1)
        self.assertNotIn("APP_RELEASE_REPOSITORY_ID", text)
        identity = WORKFLOW["jobs"]["identity"]["steps"]
        reader = next(step for step in identity if step.get("id") == "identity")
        self.assertEqual(reader["env"]["APP_RELEASE_REPOSITORY"], "${{ vars.APP_RELEASE_REPOSITORY }}")
        self.assertEqual(text.count("repositories: aios-desktop-app"), 2)
        for name in ("aios-companion-releases", "aiosxaegra", "companion-memory"):
            self.assertNotIn(name, text)

    def test_the_windows_stage_step_names_the_destination_and_holds_only_the_job_read_token(self):
        # Staging copies the newest published receipt of a generation the archive misses. It reads the public destination
        # with the job's own read-only token, which cannot write there. The publisher token never reaches this step.
        _, stage = public_read_step(WORKFLOW)
        self.assertIn("stage --platform windows-amd64", stage["run"])
        self.assertEqual(stage["env"]["APP_RELEASE_REPOSITORY"], "${{ needs.identity.outputs.release_repository }}")
        self.assertEqual(stage["env"]["PUBLIC_READ_TOKEN"], GITHUB_TOKEN_REF)
        self.assertNotIn("PUBLISH_RELEASES_TOKEN", str(stage))
        self.assertNotIn("GH_TOKEN", stage["env"])

    def test_the_job_token_is_placed_only_in_the_windows_public_read_step(self):
        # Control: the shipped placement passes every rule, so each mutant below fails for its own change alone.
        self.assertEqual(github_token_violations(WORKFLOW), [])

    def test_mutants_of_the_job_token_placement_are_each_refused(self):
        def set_env(job, step_name, key, value):
            def change(workflow):
                step = next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == step_name)
                step.setdefault("env", {})[key] = value
            return change

        def set_job_env(job, key, value):
            def change(workflow):
                workflow["jobs"][job].setdefault("env", {})[key] = value
            return change

        def add_with_token(job, step_name):
            def change(workflow):
                step = next(step for step in workflow["jobs"][job]["steps"] if step.get("name") == step_name)
                step["with"] = {"token": GITHUB_TOKEN_REF}
            return change

        def set_permissions(job_or_none, value):
            def change(workflow):
                if job_or_none is None:
                    workflow["permissions"] = value
                else:
                    workflow["jobs"][job_or_none]["permissions"] = value
            return change

        publish = "Publish only verified assets to the unpublished new-app draft"
        mutants = {
            "job token also as GH_TOKEN on the Windows stage step": set_env("build-windows", PUBLIC_READ_STEP, "GH_TOKEN", GITHUB_TOKEN_REF),
            "job token on the macOS stage step": set_env("build-macos", PUBLIC_READ_STEP, "PUBLIC_READ_TOKEN", GITHUB_TOKEN_REF),
            "job token on the Windows upload step": set_env("build-windows", publish, "PUBLIC_READ_TOKEN", GITHUB_TOKEN_REF),
            "job token at job level": set_job_env("build-windows", "PUBLIC_READ_TOKEN", GITHUB_TOKEN_REF),
            "job token under a second name on the stage step": set_env("build-windows", PUBLIC_READ_STEP, "GITHUB_TOKEN", GITHUB_TOKEN_REF),
            "publisher secret beside the public read": set_env("build-windows", PUBLIC_READ_STEP, "GH_TOKEN", "${{ secrets.PUBLISH_RELEASES_TOKEN }}"),
            "publisher secret as the public read token": set_env("build-windows", PUBLIC_READ_STEP, "PUBLIC_READ_TOKEN", "${{ secrets.PUBLISH_RELEASES_TOKEN }}"),
            "app token beside the public read": set_env("build-windows", PUBLIC_READ_STEP, "BUILDER_TOKEN", "${{ steps.source-token.outputs.token }}"),
            "job token given to a checkout as an input": add_with_token("build-windows", "Check out the pinned bundled MCP source"),
            "write permission on the Windows job": set_permissions("build-windows", {"contents": "write"}),
            "write permission at top level": set_permissions(None, {"contents": "write"}),
        }
        for name, change in mutants.items():
            with self.subTest(mutant=name):
                workflow = copy.deepcopy(WORKFLOW)
                change(workflow)
                self.assertTrue(github_token_violations(workflow), "the audit accepted this mutant")

    def test_dispatch_inputs_never_become_shell_text(self):
        for job in WORKFLOW["jobs"].values():
            for step in job["steps"]:
                self.assertNotIn("inputs.", step.get("run", ""), step.get("name"))

    def test_runner_paths_are_evaluated_in_step_context_not_invalid_job_environment(self):
        for job in WORKFLOW["jobs"].values():
            self.assertNotIn("runner.", str(job.get("env", {})))
        for job in BUILDS:
            steps = WORKFLOW["jobs"][job]["steps"]
            paths = [step["env"]["APP_ASSET_DIR"] for step in steps if "APP_ASSET_DIR" in step.get("env", {})]
            self.assertEqual(paths, ["${{ runner.temp }}/app-release-assets"] * 2)


if __name__ == "__main__":
    unittest.main()
