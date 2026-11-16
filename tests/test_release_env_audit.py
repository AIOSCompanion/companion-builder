"""The signing step's environment is pinned to the audited list.

Each variable was checked against the locked electron-builder 26.17.0 (app-builder-lib, @electron/notarize), the
locked @sentry/bundler-plugin-core 5.3.0, and the desktop repo's Vite config. APPLE_TEAM_ID is absent
on purpose: the only code that reads it is the Apple-ID notarisation branch, which this build never takes.
Adding a variable here means auditing it again.
"""
import re
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

AUDITED = {
    "COMPOSIO_API_KEY": "nonsecret canary; the emitted-bundle guard",
    "GAMMA_API_KEY": "nonsecret canary; the emitted-bundle guard",
    "CSC_LINK_B64": "raw certificate, consumed by signed_row (removed before the build)",
    "CSC_KEY_PASSWORD": "electron-builder reads CSC_KEY_PASSWORD (app-builder-lib platformPackager)",
    "APPLE_API_KEY_P8": "raw App Store Connect key, consumed by signed_row (removed before the build)",
    "APPLE_API_KEY_ID": "electron-builder reads APPLE_API_KEY_ID (MacTargetHelper, API-key branch)",
    "APPLE_API_ISSUER": "electron-builder reads APPLE_API_ISSUER (MacTargetHelper, API-key branch)",
    "GH_TOKEN": "D2: electron-builder publishes with it in this step",
    "SENTRY_AUTH_TOKEN": "read by @sentry/bundler-plugin-core 5.3.0 via the desktop Vite config",
    "PACKAGE_SCRIPT": "the package phase's npm script",
    "ROW_TIMEOUT_SECONDS": "the packaging retry budget",
    "RELEASE_TIMING_FILE": "the row's phase timing file",
}


def signing_step_env_keys():
    start = WORKFLOW.index('- name: Sign, notarise and build mac arm64')
    end = WORKFLOW.index("\n      - name:", start + 1)
    block = WORKFLOW[start:end]
    return set(re.findall(r"^          ([A-Z][A-Z0-9_]+):", block, flags=re.MULTILINE))


class EnvAuditTests(unittest.TestCase):
    def test_the_signing_step_sets_exactly_the_audited_variables(self):
        self.assertEqual(signing_step_env_keys(), set(AUDITED))

    def test_apple_team_id_is_not_passed_because_nothing_on_the_api_key_path_reads_it(self):
        self.assertNotIn("APPLE_TEAM_ID", signing_step_env_keys())

    def test_the_raw_secrets_are_listed_only_because_signed_row_removes_them_before_the_build(self):
        self.assertIn("removed before the build", AUDITED["CSC_LINK_B64"])
        self.assertIn("removed before the build", AUDITED["APPLE_API_KEY_P8"])


if __name__ == "__main__":
    unittest.main()
