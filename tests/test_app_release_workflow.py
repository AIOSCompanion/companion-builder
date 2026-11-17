from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release-app.yml").read_text())
BUILDS = {"build-windows": ("windows-2022", "windows-amd64"), "build-macos": ("macos-15", "darwin-arm64")}
APPLE_SECRETS = ("CSC_LINK", "CSC_KEY_PASSWORD", "APPLE_API_KEY", "APPLE_API_KEY_ID", "APPLE_API_ISSUER")


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
                    self.assertEqual(step["with"]["repositories"], "companion-engine")
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
        for name in ("aios-companion-releases", "aios-desktop-app", "aiosxaegra", "companion-memory"):
            self.assertNotIn(name, text)

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
