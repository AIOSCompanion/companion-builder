from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/release-app.yml").read_text())


class AppWorkflowTests(unittest.TestCase):
    def test_only_owner_dispatch_from_public_builder_main_enters_approved_jobs(self):
        trigger = WORKFLOW.get("on", WORKFLOW.get(True))
        self.assertEqual(set(trigger), {"workflow_dispatch"})
        self.assertFalse(trigger["workflow_dispatch"]["inputs"]["allow_unsigned_macos_test"]["default"])
        self.assertIn("github.ref == 'refs/heads/main'", WORKFLOW["jobs"]["identity"]["if"])
        self.assertIn("github.repository == 'AIOSCompanion/companion-builder'", WORKFLOW["jobs"]["identity"]["if"])
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
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
                if "APPLE_API_KEY" in env or "CSC_KEY_PASSWORD" in env:
                    self.assertEqual(step["name"], "Sign notarize and staple the macOS arm64 bundle")
                    self.assertIn("!inputs.allow_unsigned_macos_test", step["if"])
                if "BUILDER_APP_PRIVATE_KEY" in str(step):
                    self.assertEqual(step["with"]["repositories"], "companion-engine")
                    self.assertEqual(step["with"]["permission-contents"], "read")

    def test_private_git_output_is_sealed_and_only_hash_pins_drive_checkout(self):
        steps = WORKFLOW["jobs"]["build"]["steps"]
        private = next(step for step in steps if step.get("name") == "Check out private source at the validated immutable commit")
        self.assertIn("run_sealed.py", private["run"])
        self.assertIn("app_release.py", private["run"])
        self.assertTrue(private["run"].endswith("checkout"))
        env = WORKFLOW["jobs"]["build"]["env"]
        self.assertEqual(env["SOURCE_SHA"], "${{ needs.identity.outputs.commit_sha }}")
        self.assertEqual(env["TAG_OBJECT_SHA"], "${{ needs.identity.outputs.tag_object_sha }}")

    def test_only_sealed_logs_are_uploaded_as_actions_artifacts(self):
        for job in WORKFLOW["jobs"].values():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/upload-artifact@"):
                    self.assertTrue(step["with"]["path"].endswith("/*.gpg"))
                    self.assertEqual(step["with"]["retention-days"], 1)

    def test_both_required_native_rows_finish_before_final_draft_verification(self):
        build = WORKFLOW["jobs"]["build"]
        rows = build["strategy"]["matrix"]["include"]
        self.assertEqual(rows, [{"platform": "windows-amd64", "os": "windows-2022"},
                                {"platform": "darwin-arm64", "os": "macos-15"}])
        self.assertEqual(WORKFLOW["jobs"]["finalize"]["needs"], ["identity", "build"])
        setup = next(step for step in build["steps"] if step.get("uses") == "actions/setup-go@v5")
        self.assertEqual(setup["with"]["go-version"], "1.27.4")
        self.assertFalse(setup["with"]["cache"])
        self.assertNotIn("self-hosted", str(WORKFLOW))

    def test_runner_paths_are_evaluated_in_step_context_not_invalid_job_environment(self):
        for job in WORKFLOW["jobs"].values():
            self.assertNotIn("runner.", str(job.get("env", {})))
        steps = WORKFLOW["jobs"]["build"]["steps"]
        paths = [step["env"]["APP_ASSET_DIR"] for step in steps if "APP_ASSET_DIR" in step.get("env", {})]
        self.assertEqual(paths, ["${{ runner.temp }}/app-release-assets"] * 2)
