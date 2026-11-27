"""The relay migration dry run must stay read-only toward production, gated, and always clean up its copy.

Three layers. The workflow contract is read from the file as it is in this repository. The VM program is checked
for the production boundaries (read-only source, no container restarts, a copy-only migration). And one behavioural
test runs the real VM program with stub docker, git and timeout commands: when the migration fails, the copy must
still be dropped, the work directory removed and the database login kept out of every output.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / '.github/workflows/relay-migration-dryrun.yml'
PROGRAM = ROOT / 'scripts/relay_migration_dryrun_remote.sh'
MAIN_ONLY = "github.ref == 'refs/heads/main'"
REVISION = 'ab' * 20
TOKEN = 'ghs_synthetic_read_token_for_tests'


def trigger(workflow):
    return workflow.get('on', workflow.get(True))


def secrets_referenced(source):
    return set(re.findall(r'secrets\.([A-Z0-9_]+)', source))


class DryRunWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = WORKFLOW.read_text(encoding='utf-8')
        cls.workflow = yaml.safe_load(cls.source)
        cls.jobs = cls.workflow['jobs']

    def test_dispatch_only_with_one_exact_sha_input(self):
        self.assertEqual(set(trigger(self.workflow)), {'workflow_dispatch'})
        inputs = trigger(self.workflow)['workflow_dispatch']['inputs']
        self.assertEqual(set(inputs), {'relay_sha'})
        self.assertTrue(inputs['relay_sha']['required'])
        self.assertIn('^[a-f0-9]{40}$', self.source)

    def test_the_only_job_is_main_only_and_approval_gated_by_relay_deploy(self):
        self.assertEqual(set(self.jobs), {'dryrun'})
        job = self.jobs['dryrun']
        self.assertEqual(job.get('if'), MAIN_ONLY)
        self.assertEqual(job['environment'], 'relay-deploy')
        self.assertNotIn('needs', job)
        self.assertNotIn('outputs', job)
        self.assertEqual(job['permissions'], {'contents': 'read', 'id-token': 'write'})

    def test_concurrency_never_cancels_a_running_dry_run(self):
        self.assertEqual(self.workflow['concurrency']['group'], 'relay-migration-dryrun')
        self.assertFalse(self.workflow['concurrency']['cancel-in-progress'])

    def test_the_read_token_is_minted_in_the_job_scoped_to_aiosxaegra_and_read_only(self):
        steps = self.jobs['dryrun']['steps']
        minting = [s for s in steps if 'create-github-app-token' in s.get('uses', '')]
        self.assertEqual(len(minting), 1)
        self.assertEqual(minting[0]['with']['repositories'], 'aiosxaegra')
        self.assertEqual(minting[0]['with']['permission-contents'], 'read')
        names = [s.get('name', '') for s in steps]
        self.assertLess(names.index('Create a read-only App token for aiosxaegra'),
                        names.index('Check out aiosxaegra at the approved commit'))

    def test_the_vm_step_is_the_relay_vm_run_action_on_the_same_instance(self):
        steps = self.jobs['dryrun']['steps']
        vm = [s for s in steps if s.get('uses') == './relay/.github/actions/vm-run']
        self.assertEqual(len(vm), 1)
        self.assertEqual(vm[0]['with']['instance'], 'aiosserver-v2')
        self.assertEqual(vm[0]['with']['zone'], 'europe-north2-b')
        self.assertEqual(vm[0]['with']['project'], 'aios-460415')
        self.assertEqual(vm[0]['with']['workload_identity_provider'],
                         '${{ vars.RELAY_GCP_WORKLOAD_IDENTITY_PROVIDER }}')
        self.assertEqual(vm[0]['with']['service_account'], '${{ vars.RELAY_GCP_DEPLOY_SERVICE_ACCOUNT }}')

    def test_the_vm_program_is_this_repositorys_file_and_it_exists(self):
        vm = next(s for s in self.jobs['dryrun']['steps'] if s.get('uses') == './relay/.github/actions/vm-run')
        self.assertEqual(vm['with']['script_file'], 'scripts/relay_migration_dryrun_remote.sh')
        self.assertTrue(PROGRAM.is_file())

    def test_only_the_read_token_crosses_to_the_vm_and_no_dotenv_is_delivered(self):
        vm = next(s for s in self.jobs['dryrun']['steps'] if s.get('uses') == './relay/.github/actions/vm-run')
        names = set(vm['with']['pass_env'].split())
        self.assertEqual(names, {'DRYRUN_REVISION', 'DRYRUN_SOURCE_URL', 'GH_PULL_TOKEN'})
        self.assertNotIn('BACKEND_DOTENV_B64', self.source)

    def test_the_only_secret_is_the_builder_app_key_that_relay_deploy_already_reads(self):
        self.assertEqual(secrets_referenced(self.source), {'BUILDER_APP_PRIVATE_KEY'})

    def test_no_artifact_can_carry_private_source_or_counts_off_the_vm(self):
        self.assertNotIn('upload-artifact', self.source)
        self.assertNotIn('download-artifact', self.source)

    def test_the_outcome_is_recorded_even_when_the_dry_run_fails(self):
        steps = {s.get('name'): s for s in self.jobs['dryrun']['steps']}
        self.assertEqual(steps['Record the dry-run outcome']['if'], 'always()')


class DryRunProgramBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PROGRAM.read_text(encoding='utf-8')
        cls.lines = cls.source.splitlines()

    def heredoc(self, tag):
        match = re.search(rf"<<'{tag}'\n(.*?)\n{tag}\n", self.source, re.S)
        self.assertIsNotNone(match, tag)
        return match.group(1)

    def test_it_fails_closed_before_touching_anything(self):
        self.assertTrue(any(line == 'set -euo pipefail' for line in self.lines[:30]))
        self.assertLess(self.source.index('exit 64'), self.source.index('trap cleanup EXIT'))

    def test_the_cleanup_trap_is_armed_before_the_copy_is_created(self):
        self.assertLess(self.source.index('trap cleanup EXIT'), self.source.index('CREATE DATABASE'))

    def test_drop_database_appears_only_in_the_copy_drop_helper(self):
        self.assertEqual(self.source.count('DROP DATABASE'), 1)
        helper = self.source[self.source.index('drop_copy() {'):self.source.index('cleanup() {')]
        self.assertIn('DROP DATABASE', helper)
        self.assertIn('COPY_PATTERN', helper)

    def test_the_drop_runs_from_the_exit_path_and_a_failed_drop_fails_the_run(self):
        cleanup = self.source[self.source.index('cleanup() {'):self.source.index('trap cleanup EXIT')]
        self.assertIn('drop_copy "$COPY"', cleanup)
        self.assertIn('status=1', cleanup)

    def test_the_production_database_is_named_only_in_the_read_only_dump(self):
        uses = [line for line in self.lines if '$PG_PRODUCTION_DB' in line]
        self.assertEqual(len(uses), 1)
        self.assertIn('pg_dump', uses[0])
        self.assertIn('--no-owner', uses[0])
        self.assertNotIn('pg_restore', uses[0])

    def test_no_production_container_is_restarted_stopped_recreated_or_reached_through_compose(self):
        for forbidden in ('docker restart', 'docker stop', 'docker kill', 'docker start', 'docker compose',
                          'docker-compose', 'force-recreate', 'up -d', '/home/ibrahim', 'docker rm -f aegra'):
            self.assertNotIn(forbidden, self.source, forbidden)
        for line in self.lines:
            if 'docker' in line:
                self.assertIsNone(re.search(r'\baegra\b', line), line)

    def test_no_migration_or_data_statement_is_sent_to_production(self):
        for statement in ('ALTER TABLE', 'INSERT INTO', 'DELETE FROM', 'TRUNCATE', 'DROP TABLE'):
            self.assertNotIn(statement, self.source, statement)

    def test_the_probe_is_read_only_and_refuses_any_database_but_the_copy(self):
        probe = self.heredoc('PROBE')
        self.assertIn('default_transaction_read_only=on', probe)
        self.assertIn('database != EXPECTED_DB', probe)
        self.assertNotIn('INSERT', probe)
        self.assertNotIn('UPDATE ', probe)
        self.assertNotIn('DELETE', probe)

    def test_the_embedded_programs_compile(self):
        for tag in ('PROBE', 'SUMMARY'):
            compile(self.heredoc(tag), tag, 'exec')

    def test_the_copy_name_is_fixed_and_timestamped(self):
        self.assertIn("COPY_PATTERN='^relay_migration_dryrun_[0-9]{14}$'", self.source)


def find_bash():
    if os.name == 'nt':
        for candidate in (r'C:\Program Files\Git\bin\bash.exe', r'C:\Program Files\Git\usr\bin\bash.exe'):
            if os.path.isfile(candidate):
                return candidate
        return None
    return shutil.which('bash')


def msys(path):
    """A path bash can use on every platform: /c/... for a Windows drive, unchanged elsewhere."""
    text = Path(path).resolve().as_posix()
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


STUB_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  inspect)
    case "$*" in
      *State.Running*) echo true ;;
      *) echo aiosxaegra_postgres_data ;;
    esac ;;
  exec)
    case "$*" in
      *printenv*) echo stub-password ;;
      *pg_restore*) cat > /dev/null ;;
    esac ;;
  run)
    case "$*" in
      *heads*) echo "c20261126_sign_out_everywhere (head)" ;;
      *python*) echo '{}' ;;
      *upgrade*) exit 1 ;;
    esac ;;
esac
exit 0
'''

STUB_GIT = r'''#!/usr/bin/env bash
echo "git $*" >> "$STUB_LOG"
case "$*" in
  *rev-parse*) echo "$DRYRUN_REVISION" ;;
  *archive*) tar -cf - -T /dev/null ;;
esac
exit 0
'''

STUB_TIMEOUT = r'''#!/usr/bin/env bash
if [[ "$1" == --* ]]; then shift; fi
shift
exec "$@"
'''


def write_stub(path, body):
    path.write_text(body, encoding='utf-8', newline='\n')
    os.chmod(path, 0o755)


class FailedMigrationStillDropsTheCopyTests(unittest.TestCase):
    def test_a_failed_migration_drops_the_copy_and_keeps_the_login_out_of_every_output(self):
        bash = find_bash()
        if bash is None:
            self.skipTest('no bash available to run the VM program')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            stubs = tmp / 'bin'
            stubs.mkdir()
            state = tmp / 'state'
            state.mkdir()
            log = tmp / 'calls.log'
            log.write_text('', encoding='utf-8')
            write_stub(stubs / 'docker', STUB_DOCKER)
            write_stub(stubs / 'git', STUB_GIT)
            write_stub(stubs / 'timeout', STUB_TIMEOUT)
            write_stub(stubs / 'python3', f'#!/usr/bin/env bash\nexec "{msys(sys.executable)}" "$@"\n')
            env = {
                'PATH': '/usr/bin:/bin',
                'HOME': msys(tmp),
                'STUB_DIR': msys(stubs),
                'PROGRAM': msys(PROGRAM),
                'DRYRUN_REVISION': REVISION,
                'DRYRUN_SOURCE_URL': 'https://github.com/AIOSCompanion/aiosxaegra',
                'GH_PULL_TOKEN': TOKEN,
                'RELAY_DRYRUN_STATE': msys(state),
                'STUB_LOG': msys(log),
            }
            # Prepend the stubs inside bash, then source the program in that same shell: Git Bash puts its own tool
            # directories ahead of any inherited PATH, including when a second bash starts.
            launch = 'PATH="$STUB_DIR:$PATH"; export PATH; . "$PROGRAM"'
            result = subprocess.run([bash, '-c', launch], env=env, capture_output=True, text=True, timeout=120)
            calls = log.read_text(encoding='utf-8')

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        created = re.search(r'CREATE DATABASE \\?"?(relay_migration_dryrun_\d{14})', calls)
        self.assertIsNotNone(created, calls + result.stdout + result.stderr)
        copy = created.group(1)
        self.assertIn(f'DROP DATABASE IF EXISTS "{copy}"', calls)
        self.assertLess(calls.index('alembic upgrade head'), calls.index('DROP DATABASE'))
        self.assertIn('docker rm -f relay-dryrun-alembic', calls)
        self.assertIn(f'docker image rm -f relay-migration-dryrun:{REVISION[:12]}', calls)
        self.assertEqual(list(state.glob('run.*')), [], 'the work directory must be removed')
        combined = result.stdout + result.stderr + calls
        self.assertNotIn(TOKEN, combined)
        self.assertNotIn('stub-password', combined)


if __name__ == '__main__':
    unittest.main()
