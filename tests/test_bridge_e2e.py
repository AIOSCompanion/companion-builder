"""The bridge e2e's checks, on fake folders and registry entries, and the workflow's order and gating.

The Windows run itself happens only on the runner (scripts/bridge_e2e.py). These tests pin what each check accepts and refuses,
so a check that would pass a broken install is caught here.
"""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import bridge_e2e  # noqa: E402
from bridge_e2e import (BRIDGE_UNINSTALL_GUID, IMPORT_FINISHED, PROGRAM, Run, check_feed_served,  # noqa: E402
                        check_go_app, check_import_pending, matching_pids, check_old_app_gone, check_old_data_detected,
                        check_stable_install, check_uninstall, check_update_path, point_feed_at, read_receipt,
                        seed_old_data, seed_paths, serve_feed, takeover_exit_line)
import urllib.request  # noqa: E402

VERSION = "0.19.5"
INSTALLER = "Companion-Setup-x64-0.19.5.exe"


class PointFeedTests(unittest.TestCase):
    def test_the_installed_feed_moves_to_the_local_generic_server_and_keeps_other_keys(self):
        before = ("owner: AIOSCompanion\nrepo: aios-companion-releases\nprovider: github\n"
                  "updaterCacheDirName: companion-updater\n")
        after = point_feed_at(before, "http://127.0.0.1:50123")
        self.assertIn("provider: generic\n", after)
        self.assertIn("url: http://127.0.0.1:50123\n", after)
        self.assertNotIn("provider: github", after)
        self.assertNotIn("owner:", after)
        self.assertNotIn("repo:", after)
        self.assertIn("updaterCacheDirName: companion-updater", after)

    def test_only_a_loopback_feed_is_accepted(self):
        with self.assertRaises(bridge_e2e.E2EError):
            point_feed_at("provider: github\n", "https://example.com")


class SeedTests(unittest.TestCase):
    def test_the_seed_is_written_where_the_engine_looks_and_is_detected(self):
        with tempfile.TemporaryDirectory() as roaming:
            self.assertFalse(check_old_data_detected(Path(roaming))[0])
            seed_old_data(Path(roaming))
            events, companion = seed_paths(Path(roaming))
            self.assertEqual(events.parts[-3:], ("aegra-local", "eventlog", "events.db"))
            self.assertEqual(companion.parts[-2:], ("aegra-local", "companion.db"))
            self.assertTrue(check_old_data_detected(Path(roaming))[0])
            with contextlib.closing(sqlite3.connect(events)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)

    def test_detection_needs_the_event_log_the_importer_looks_for(self):
        with tempfile.TemporaryDirectory() as roaming:
            seed_old_data(Path(roaming))
            events, _ = seed_paths(Path(roaming))
            events.unlink()
            self.assertFalse(check_old_data_detected(Path(roaming))[0])


class StableInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stable = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_receipt(self, version):
        (self.stable / "companion-install.json").write_text(json.dumps({"schema": 1, "version": version, "files": {}}))

    def test_a_receipt_and_the_cli_both_at_the_bridge_version_pass(self):
        self.write_receipt(VERSION)
        self.assertTrue(check_stable_install(self.stable, VERSION, VERSION + "\n")[0])

    def test_an_old_receipt_fails(self):
        self.write_receipt("0.18.0")
        self.assertFalse(check_stable_install(self.stable, VERSION, None)[0])

    def test_a_missing_receipt_fails(self):
        self.assertFalse(check_stable_install(self.stable, VERSION, None)[0])

    def test_a_cli_reporting_another_version_fails_even_with_a_matching_receipt(self):
        self.write_receipt(VERSION)
        self.assertFalse(check_stable_install(self.stable, VERSION, "0.18.0\n")[0])

    def test_old_program_files_at_the_stable_path_fail_the_old_app_gone_check(self):
        (self.stable / "Uninstall Companion.exe").write_text("x")
        self.assertFalse(check_old_app_gone(self.stable)[0])
        (self.stable / "Uninstall Companion.exe").unlink()
        self.assertTrue(check_old_app_gone(self.stable)[0])


def entry(version, location, uninstall="\"C:\\x\\Companion.exe\" --uninstall", name="Companion"):
    return {"DisplayName": name, "DisplayVersion": version, "InstallLocation": location, "UninstallString": uninstall}


class UninstallTests(unittest.TestCase):
    STABLE = Path(r"C:\Users\u\AppData\Local\Programs\Companion")

    def test_the_bridge_entry_alone_passes(self):
        entries = [(BRIDGE_UNINSTALL_GUID, entry(VERSION, str(self.STABLE)))]
        self.assertTrue(check_uninstall(entries, VERSION, self.STABLE)[0])

    def test_a_key_still_naming_the_old_version_fails(self):
        entries = [(BRIDGE_UNINSTALL_GUID, entry("0.18.0", str(self.STABLE)))]
        self.assertFalse(check_uninstall(entries, VERSION, self.STABLE)[0])

    def test_a_second_companion_entry_fails(self):
        entries = [(BRIDGE_UNINSTALL_GUID, entry(VERSION, str(self.STABLE))),
                   ("{OLD-GUID}", entry("0.18.0", str(self.STABLE)))]
        ok, detail = check_uninstall(entries, VERSION, self.STABLE)
        self.assertFalse(ok)
        self.assertIn("another Companion entry", detail)

    def test_a_missing_bridge_key_fails(self):
        self.assertFalse(check_uninstall([], VERSION, self.STABLE)[0])

    def test_an_uninstall_string_without_the_uninstall_switch_fails(self):
        entries = [(BRIDGE_UNINSTALL_GUID, entry(VERSION, str(self.STABLE), uninstall='"C:\\x\\Companion.exe"'))]
        self.assertFalse(check_uninstall(entries, VERSION, self.STABLE)[0])


class UpdatePathTests(unittest.TestCase):
    GOOD = ('start: args=["--updated"]\ninstall version 0.19.5 into X\ninstall complete: 0.19.5\nexit 0\n')

    def test_a_bridge_started_by_the_updater_that_finished_with_exit_zero_passes(self):
        self.assertTrue(check_update_path(self.GOOD)[0])

    def test_a_run_without_updated_fails(self):
        self.assertFalse(check_update_path(self.GOOD.replace("--updated", "--other"))[0])

    def test_a_failed_run_fails(self):
        text = 'start: args=["--updated"]\nexit 4: the install failed\n'
        self.assertFalse(check_update_path(text)[0])


class FeedTests(unittest.TestCase):
    def test_the_update_needs_both_the_feed_and_the_installer_fetched(self):
        ok = [("/latest.yml", "200"), ("/" + INSTALLER, "200")]
        self.assertTrue(check_feed_served(ok, INSTALLER)[0])
        self.assertFalse(check_feed_served([("/latest.yml", "200")], INSTALLER)[0])
        self.assertFalse(check_feed_served([("/" + INSTALLER, "200")], INSTALLER)[0])
        self.assertFalse(check_feed_served([("/latest.yml", "404"), ("/" + INSTALLER, "200")], INSTALLER)[0])


class ImportPendingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "companion-engine"
        self.home.mkdir()
        self.kept = Path(self.tmp.name) / "Companion.takeover-previous-abc"
        self.kept.mkdir()
        self.record = self.home / "takeover-previous.json"
        self.record.write_text(json.dumps({"version": 1, "installs": [{"install_dir": "x", "previous": str(self.kept)}]}))
        self.launched = time.time() - 5
        (self.home / "engine.log").write_text("started\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_check(self, logs="started\n", old_data=True):
        return check_import_pending(self.home, self.launched, self.record, old_data, logs)

    def test_no_finish_line_with_the_previous_folder_and_data_kept_is_pending(self):
        ok, detail = self.run_check()
        self.assertTrue(ok)
        self.assertIn("pending", detail)

    def test_a_finish_line_fails_because_the_runner_cannot_sign_in(self):
        self.assertFalse(self.run_check(logs=f"{IMPORT_FINISHED} status=ok\n")[0])

    def test_a_removed_previous_folder_fails(self):
        self.kept.rmdir()
        self.assertFalse(self.run_check()[0])

    def test_removed_previous_data_fails(self):
        self.assertFalse(self.run_check(old_data=False)[0])

    def test_an_engine_that_wrote_nothing_after_launch_fails(self):
        self.launched = time.time() + 60
        self.assertFalse(self.run_check()[0])


class WorkflowTests(unittest.TestCase):
    WORKFLOW = yaml.safe_load((ROOT / ".github/workflows/bridge-release.yml").read_text(encoding="utf-8"))
    JOB = WORKFLOW["jobs"]["bridge-windows"]

    def steps(self):
        return self.JOB["steps"]

    def index(self, predicate):
        return next(i for i, step in enumerate(self.steps()) if predicate(step))

    def test_the_job_env_names_no_runner_context(self):
        self.assertNotIn("runner.", str(self.JOB.get("env", {})))

    def test_the_run_folders_are_exported_to_the_later_steps(self):
        export = self.steps()[self.index(lambda s: "BRIDGE_OUT_DIR=" in s.get("run", ""))]["run"]
        for name in ("BRIDGE_INPUT_DIR", "BRIDGE_OUT_DIR", "E2E_OUT_DIR"):
            self.assertIn(f"{name}=", export)

    def test_the_e2e_runs_after_the_build_and_before_the_draft(self):
        build = self.index(lambda s: 'bridge_release.py" build' in s.get("run", ""))
        e2e = self.index(lambda s: "bridge_e2e.py" in s.get("run", ""))
        draft = self.index(lambda s: 'bridge_release.py" draft' in s.get("run", ""))
        self.assertLess(build, e2e)
        self.assertLess(e2e, draft)

    def test_the_e2e_is_sealed_and_holds_no_publishing_credential(self):
        step = self.steps()[self.index(lambda s: "bridge_e2e.py" in s.get("run", ""))]
        self.assertIn("run_sealed.py", step["run"])
        self.assertIn("--seal-dir", step["run"])
        self.assertNotIn("PUBLISH_RELEASES_TOKEN", str(step.get("env", {})))
        self.assertNotIn("BUILDER_APP_PRIVATE_KEY", str(step.get("env", {})))

    def test_the_draft_has_no_condition_so_it_runs_only_after_a_passing_e2e(self):
        draft = self.steps()[self.index(lambda s: 'bridge_release.py" draft' in s.get("run", ""))]
        self.assertNotIn("if", draft)
        self.assertNotIn("continue-on-error", draft)
        e2e = self.steps()[self.index(lambda s: "bridge_e2e.py" in s.get("run", ""))]
        self.assertNotIn("continue-on-error", e2e)

    def test_the_sealed_e2e_output_is_kept_three_days_and_uploaded_even_on_failure(self):
        upload = self.steps()[self.index(lambda s: s.get("name", "").startswith("Upload the sealed e2e"))]
        self.assertEqual(upload["with"]["retention-days"], 3)
        self.assertEqual(upload.get("if"), "always()")
        self.assertIn("bridge-e2e-e2e-out", upload["with"]["path"])

    def test_the_workflow_still_never_publishes(self):
        text = (ROOT / ".github/workflows/bridge-release.yml").read_text(encoding="utf-8")
        for forbidden in ("release edit", "--draft=false", "--latest", "--prerelease=false", "release publish"):
            self.assertNotIn(forbidden, text)


INSTALLER = "Companion-Setup-x64-0.19.5.exe"
TAKEOVER_FAILED = ('start: args=["--updated" "--force-run"]\n'
                   'install version 0.19.5 into C:\\Users\\u\\AppData\\Local\\Programs\\Companion\n'
                   'exit 4: The install failed: rename C:\\Users\\u\\AppData\\Local\\Programs\\Companion.takeover-staging -> '
                   'C:\\Users\\u\\AppData\\Local\\Programs\\Companion: Access is denied. The previous Companion app was put back.\n')


class FeedRequestTests(unittest.TestCase):
    """The incident: electron-updater fetched /latest.yml?noCache=... and the check, which compared the raw request line,
    reported that the feed was never fetched while the bridge had run from it. These use a real socket, as the runner does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        (self.folder / "latest.yml").write_text("version: 0.19.5\n", encoding="utf-8")
        (self.folder / INSTALLER).write_bytes(b"installer-bytes")
        self.server, self.base, self.records = serve_feed(self.folder)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_an_electron_style_cache_busting_fetch_of_latest_yml_counts_as_served(self):
        urllib.request.urlopen(f"{self.base}/latest.yml?noCache=1k2j3x").read()
        urllib.request.urlopen(f"{self.base}/{INSTALLER}").read()
        self.assertIn(("/latest.yml?noCache=1k2j3x", "200"), self.records)
        ok, detail = check_feed_served(self.records, INSTALLER)
        self.assertTrue(ok, detail)

    def test_the_raw_request_line_comparison_that_missed_it_is_refused_by_the_fixed_check(self):
        # The old comparison, kept here as the mutant: it must not pass the same traffic.
        def raw_compare(records, installer_name):
            latest = any(path.endswith("/latest.yml") and code == "200" for path, code in records)
            return latest
        urllib.request.urlopen(f"{self.base}/latest.yml?noCache=1k2j3x").read()
        urllib.request.urlopen(f"{self.base}/{INSTALLER}").read()
        self.assertFalse(raw_compare(self.records, INSTALLER))
        self.assertTrue(check_feed_served(self.records, INSTALLER)[0])

    def test_a_fetch_of_another_file_does_not_count_as_latest_yml(self):
        urllib.request.urlopen(f"{self.base}/{INSTALLER}").read()
        self.assertFalse(check_feed_served(self.records, INSTALLER)[0])


class GoAppIdentityTests(unittest.TestCase):
    """The Go app is judged over the window: a pid that runs the stable program with the receipt's bytes in every sample."""
    STABLE = Path("/tmp/Programs/Companion")
    RECEIPT = {"schema": 1, "version": VERSION, "files": {PROGRAM: "ab" * 32, "companion-cli.exe": "cd" * 32}}
    WINDOW = [{4321}] * 13

    def check(self, samples=WINDOW, receipt=RECEIPT, home=True, version=VERSION):
        return check_go_app(self.STABLE, version, samples, receipt, home)

    def test_an_app_the_takeover_started_counts_as_running_and_no_second_copy_is_needed(self):
        # The takeover launched the single-instance app as pid 4321; this run only samples it, and the pid is recorded.
        ok, detail = self.check()
        self.assertTrue(ok, detail)
        self.assertIn("4321", detail)

    def test_the_pid_must_be_present_in_every_sample_of_the_window(self):
        samples = [{4321}] * 6 + [set()] + [{4321}] * 6
        self.assertFalse(self.check(samples)[0])

    def test_a_replacement_pid_does_not_stand_in_for_the_one_that_stopped(self):
        self.assertFalse(self.check([{1}] * 6 + [{2}] * 7)[0])

    def test_no_matching_process_at_any_sample_fails(self):
        self.assertFalse(self.check([set()] * 13)[0])

    def test_a_rolled_back_install_with_no_receipt_for_the_bridge_fails(self):
        ok, detail = self.check(receipt=None)
        self.assertFalse(ok)
        self.assertIn("no install receipt", detail)

    def test_a_receipt_naming_another_version_fails(self):
        self.assertFalse(self.check(receipt={**self.RECEIPT, "version": "0.18.0"})[0])

    def test_a_missing_home_folder_fails(self):
        self.assertFalse(self.check(home=False)[0])

    def test_an_empty_window_fails(self):
        self.assertFalse(self.check([])[0])


class MatchingProcessTests(unittest.TestCase):
    STABLE = Path("/tmp/Programs/Companion")
    GOOD = {str(STABLE / PROGRAM): "ab" * 32}

    def sha_table(self, table):
        calls = []

        def sha_of(image):
            calls.append(image)
            return table.get(image)
        return sha_of, calls

    def test_the_stable_program_with_the_receipt_bytes_matches(self):
        sha_of, _ = self.sha_table(self.GOOD)
        self.assertEqual(matching_pids([(10, str(self.STABLE / PROGRAM))], self.STABLE, "ab" * 32, sha_of), {10})

    def test_an_old_program_at_the_same_path_with_other_bytes_does_not_match(self):
        sha_of, _ = self.sha_table({str(self.STABLE / PROGRAM): "0e" * 32})
        self.assertEqual(matching_pids([(10, str(self.STABLE / PROGRAM))], self.STABLE, "ab" * 32, sha_of), set())

    def test_another_program_path_with_the_same_bytes_does_not_match(self):
        sha_of, _ = self.sha_table({"/tmp/old/Companion.exe": "ab" * 32})
        self.assertEqual(matching_pids([(10, "/tmp/old/Companion.exe")], self.STABLE, "ab" * 32, sha_of), set())

    def test_a_process_whose_path_cannot_be_read_does_not_match(self):
        sha_of, _ = self.sha_table(self.GOOD)
        self.assertEqual(matching_pids([(10, None)], self.STABLE, "ab" * 32, sha_of), set())

    def test_without_a_receipt_hash_nothing_matches(self):
        sha_of, _ = self.sha_table(self.GOOD)
        self.assertEqual(matching_pids([(10, str(self.STABLE / PROGRAM))], self.STABLE, None, sha_of), set())

    def test_the_file_is_hashed_only_for_a_process_at_the_stable_path(self):
        sha_of, calls = self.sha_table(self.GOOD)
        matching_pids([(1, "/tmp/elsewhere/Companion.exe")], self.STABLE, "ab" * 32, sha_of)
        self.assertEqual(calls, [])


class TakeoverExitLineTests(unittest.TestCase):
    def test_the_last_exit_line_is_the_takeover_own_words(self):
        self.assertEqual(takeover_exit_line(TAKEOVER_FAILED).split(":")[0], "exit 4")
        self.assertTrue(takeover_exit_line(TAKEOVER_FAILED).startswith("exit 4: The install failed"))

    def test_a_log_without_an_exit_line_gives_nothing(self):
        self.assertEqual(takeover_exit_line('start: args=["--updated"]\ninstall complete\n'), "")

    def test_a_failed_takeover_leads_the_result_with_its_exit_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(Path(tmp))
            run.takeover_line = takeover_exit_line(TAKEOVER_FAILED)
            run.check("bridge_ran_from_old_updater", False, "the bridge log has no 'install complete'")
            run.check("go_app_launches", False, "the running process is old")
            run.write_result()
            document = json.loads((Path(tmp) / "e2e-result.json").read_text(encoding="utf-8"))
            self.assertEqual(list(document)[0], "summary")
            self.assertEqual(document["summary"], run.takeover_line)
            self.assertEqual(document["takeover_exit"], run.takeover_line)
            self.assertFalse(document["passed"])
            self.assertEqual((Path(tmp) / "e2e-summary.txt").read_text(encoding="utf-8").strip(), run.takeover_line)

    def test_without_a_takeover_failure_the_first_failing_check_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Run(Path(tmp))
            run.check("stable_path_has_go_version", False, "receipt version '0.18.0', expected '0.19.5'")
            self.assertTrue(run.summary().startswith("First failing check: stable_path_has_go_version"))

    def test_a_receipt_that_does_not_read_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(read_receipt(Path(tmp)))
            (Path(tmp) / "companion-install.json").write_text("not json", encoding="utf-8")
            self.assertIsNone(read_receipt(Path(tmp)))
            (Path(tmp) / "companion-install.json").write_text('{"version": "0.19.5", "files": {}}', encoding="utf-8")
            self.assertEqual(read_receipt(Path(tmp))["version"], "0.19.5")


if __name__ == "__main__":
    unittest.main()
