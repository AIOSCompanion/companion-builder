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

    def test_the_job_timeout_covers_the_worst_case_of_every_phase_bound(self):
        constants = dict(re.findall(r'^([A-Z_]+_LIMIT)=([0-9]+)$', PROGRAM.read_text(encoding='utf-8'), re.M))
        for name in ('FETCH', 'PROD', 'HEADS', 'PG', 'BUILD', 'SCHEMA', 'DATA', 'MIGRATE', 'PROBE'):
            self.assertIn(f'{name}_LIMIT', constants)
        n = {name: int(constants[f'{name}_LIMIT']) for name in
             ('FETCH', 'PROD', 'HEADS', 'PG', 'BUILD', 'SCHEMA', 'DATA', 'MIGRATE', 'PROBE')}
        # Each phase counted with its multiplicity in the program (see the comment above the limits).
        budget = (n['FETCH'] + 6 * n['PROD'] + n['HEADS'] + n['BUILD'] + n['SCHEMA'] + n['DATA']
                  + 3 * n['MIGRATE'] + 3 * n['PROBE'] + 4 * n['PG'])
        self.assertLessEqual(budget, int(self.workflow['jobs']['dryrun']['timeout-minutes']) * 60)

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

    def test_the_production_database_is_named_only_in_the_read_only_helper_and_the_two_dumps(self):
        uses = [line for line in self.lines if '$PG_PRODUCTION_DB' in line]
        helper = [line for line in uses if 'pg_dump' not in line]
        dumps = [line for line in uses if 'pg_dump' in line]
        self.assertEqual(len(helper), 1, uses)
        self.assertIn('psql -X -q', helper[0])
        self.assertEqual(len(dumps), 2, uses)
        for line in dumps:
            self.assertIn('--no-owner', line)
            self.assertNotIn('pg_restore', line)

    def test_no_production_container_is_restarted_stopped_recreated_or_reached_through_compose(self):
        for forbidden in ('docker restart', 'docker stop', 'docker kill', 'docker start', 'docker compose',
                          'docker-compose', 'force-recreate', 'up -d', '/home/ibrahim', 'docker rm -f aegra'):
            self.assertNotIn(forbidden, self.source, forbidden)
        for line in self.lines:
            if 'docker' in line:
                self.assertIsNone(re.search(r'\baegra\b', line), line)

    def test_no_migration_or_data_statement_is_sent_to_production(self):
        # The embedded programs (derive, probe, summary) are files the VM writes and runs; they are not sent to
        # production. Only the shell's own commands are checked.
        shell = re.sub(r"<<'(\w+)'\n.*?\n\1\n", '', self.source, flags=re.S)
        for statement in ('ALTER TABLE', 'INSERT INTO', 'DELETE FROM', 'TRUNCATE', 'DROP TABLE'):
            self.assertNotIn(statement, shell, statement)

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

    def test_the_disk_guard_runs_before_anything_is_created_copied_or_built_and_again_before_the_restore(self):
        order = [self.source.index('mktemp -d'),
                 self.source.index('git --git-dir="$CACHE" archive'),
                 self.source.index('python3 -I -B "$WORK/derive.py"'),
                 self.source.index('check_disk "before anything is copied or built"'),
                 self.source.index('stale="$(docker ps'),
                 self.source.index('docker build --quiet'),
                 self.source.index('CREATE DATABASE'),
                 self.source.index('check_disk "before the restore"'),
                 self.source.index('pg_dump -U "$PG_USER" -d "$PG_PRODUCTION_DB" -s')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(self.source.count('check_disk "'), 2)

    def test_the_disk_thresholds_are_the_agreed_ones(self):
        self.assertIn('DISK_HEADROOM=$((3 * GIB))', self.source)
        self.assertIn('DOCKER_MIN_FREE=$((6 * GIB))', self.source)
        self.assertIn('required=$(( 2 * db_bytes + DISK_HEADROOM ))', self.source)

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


GIB = 1024 ** 3
DERIVE_SOURCE = (ROOT / 'scripts/relay_migration_dryrun_remote.sh').read_text(encoding='utf-8')


def derive_program():
    match = re.search(r"<<'DERIVE'\n(.*?)\nDERIVE\n", DERIVE_SOURCE, re.S)
    assert match, 'the DERIVE program is missing'
    return match.group(1) + '\n'


def migration(revision, down, body):
    return f'"""fixture migration"""\n\nrevision = "{revision}"\ndown_revision = "{down}"\n\n{body}\n'


# The four revisions, shaped as the approved commit's files are (statements and a prose mention of "runs").
FIXTURE_MIGRATIONS = {
    '20261126120000_phone_simple.py': migration(
        'x20261126_phone_simple', 'w20261031_usage_turn_ts',
        'DDL = ["ALTER TABLE phone_chat_event ADD COLUMN recipients_json TEXT"]'),
    '20261126150000_receipts_owner_fk.py': migration(
        'a20261126_receipts_owner', 'x20261126_phone_simple',
        'DROP = ["ALTER TABLE link_state_admission DROP CONSTRAINT IF EXISTS link_state_admission_owner_uid_fkey"]\n'
        'RESTORE = ["ALTER TABLE link_state_admission ADD CONSTRAINT k FOREIGN KEY (owner_uid) '
        'REFERENCES link_account_chain(owner_uid) NOT VALID"]'),
    '20261126160000_chain_devices_to_account_rows.py': migration(
        'b20261126_chain_import', 'a20261126_receipts_owner',
        'SQL = "SELECT owner_uid, chain_json, account_revoked_json FROM link_account_chain WHERE owner_uid > :after"\n'
        'INS = "INSERT INTO account_devices (owner_uid, device_id) SELECT o, d FROM unnest(:o) AS v(o, d)"\n'
        'OWN = "INSERT INTO account_device_owner (owner_uid, revision) VALUES (:o, 0)"\n'
        'RESCOPE = "UPDATE phone_push_subscription SET chain_id = \'account\' WHERE chain_id IS DISTINCT FROM \'account\'"\n'
        '"""Every statement runs under this revision\'s lock_timeout; each revision commits separately."""'),
    '20261126170000_sign_out_everywhere.py': migration(
        'c20261126_sign_out_everywhere', 'b20261126_chain_import',
        'DDL = ["ALTER TABLE account_device_owner ADD COLUMN IF NOT EXISTS sessions_revoked_at BIGINT"]'),
}

# Production's public tables as pg_class reports them: name, total size in bytes, planner estimate.
FIXTURE_INVENTORY = [
    ('account_devices', 1 * GIB, 100), ('account_device_owner', 1 * GIB, 100),
    ('link_account_chain', 1 * GIB, 100), ('phone_push_subscription', 1 * GIB, 100),
    ('link_state_admission', 1 * GIB, 100), ('alembic_version', 8192, 1),
    ('phone_chat_event', 50 * GIB, 900000), ('relay_events', 60 * GIB, 9000000),
    ('runs', 40 * GIB, 500000),
]


def inventory_text(rows=FIXTURE_INVENTORY):
    return ''.join(f'{name}\t{size}\t{estimate}\n' for name, size, estimate in rows)


def write_fixture_tree(root, migrations=FIXTURE_MIGRATIONS):
    versions = root / 'alembic' / 'versions'
    versions.mkdir(parents=True)
    for name, text in migrations.items():
        (versions / name).write_text(text, encoding='utf-8', newline='\n')
    return root


class DeriveTests(unittest.TestCase):
    """The selection comes from the statements in the approved commit's own revisions."""

    def derive(self, migrations=FIXTURE_MIGRATIONS, inventory=None, start='w20261031_usage_turn_ts',
               head='c20261126_sign_out_everywhere'):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            tree = write_fixture_tree(tmp / 'src', migrations)
            table_file = tmp / 'inventory.tsv'
            table_file.write_text(inventory if inventory is not None else inventory_text(), encoding='utf-8')
            out = tmp / 'out'
            out.mkdir()
            program = tmp / 'derive.py'
            program.write_text(derive_program(), encoding='utf-8', newline='\n')
            result = subprocess.run([sys.executable, '-I', '-B', str(program), str(tree), start, head,
                                     str(table_file), str(out)], capture_output=True, text=True, timeout=120)
            files = {p.name: p.read_text(encoding='utf-8') for p in out.iterdir()}
        return result, files

    def test_selects_the_tables_the_statements_read_or_write_and_the_verifier_reads(self):
        result, files = self.derive()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(files['data-tables.txt'].split(), [
            'account_device_owner', 'account_devices', 'alembic_version', 'link_account_chain',
            'link_state_admission', 'phone_push_subscription'])
        self.assertEqual(files['schema-tables.tsv'], 'phone_chat_event\t53687091200\t900000\n')
        self.assertEqual(files['selected-bytes.txt'], str(5 * GIB + 8192))
        self.assertIn('"prose_only_not_copied": ["runs"]', files['plan.json'])

    def test_a_table_only_named_in_prose_is_not_copied(self):
        result, files = self.derive()
        self.assertNotIn('runs', files['data-tables.txt'].split())
        self.assertNotIn('runs', files['schema-tables.tsv'])
        self.assertIn('named only in comments or docstrings, not copied: runs', result.stdout)

    def test_a_table_name_built_at_run_time_refuses_the_selection(self):
        migrations = dict(FIXTURE_MIGRATIONS)
        migrations['20261126170000_sign_out_everywhere.py'] = migration(
            'c20261126_sign_out_everywhere', 'b20261126_chain_import',
            'DDL = [f"ALTER TABLE {name} ADD COLUMN x TEXT"]')
        result, _ = self.derive(migrations=migrations)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('builds a table name at run time', result.stderr)

    def test_an_alter_of_a_table_production_does_not_have_refuses_the_selection(self):
        migrations = dict(FIXTURE_MIGRATIONS)
        migrations['20261126120000_phone_simple.py'] = migration(
            'x20261126_phone_simple', 'w20261031_usage_turn_ts', 'DDL = ["ALTER TABLE ghost_table ADD COLUMN x TEXT"]')
        result, _ = self.derive(migrations=migrations)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('alters a table production does not have', result.stderr)

    def test_a_head_that_does_not_descend_from_the_start_refuses_the_selection(self):
        result, _ = self.derive(start='not_a_revision_of_this_chain')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('does not descend', result.stderr)

    def test_a_missing_verifier_table_refuses_the_selection(self):
        rows = [row for row in FIXTURE_INVENTORY if row[0] != 'link_account_chain']
        result, _ = self.derive(inventory=inventory_text(rows))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('the verifier cannot run', result.stderr)

    def test_a_malformed_inventory_refuses_the_selection(self):
        result, _ = self.derive(inventory='account_devices\tnot-a-size\t1\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('non-numeric size', result.stderr)


# Stub commands for the VM program. Every production read goes through the stubbed docker exec, so the tests can
# check which statements reach production and what they answer.
STUB_DOCKER = r'''#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  inspect)
    case "$*" in
      *State.Running*) echo true ;;
      *) echo aiosxaegra_postgres_data ;;
    esac ;;
  volume)
    echo "$STUB_VOLUME_MOUNT" ;;
  info)
    echo "$STUB_DOCKER_ROOT" ;;
  exec)
    case "$*" in
      *printenv*) echo stub-password ;;
      *pg_restore*) cat > /dev/null ;;
      *pg_dump*) : ;;
      *server_version*) echo "17.0 (170000)" ;;
      *pg_database_size*) echo "$STUB_DB_SIZE" ;;
      *pg_class*) cat "$STUB_INVENTORY" ;;
      *count\(\**) echo "$STUB_COUNT" ;;
      *" df "*)
        n=$(cat "$STUB_LOG.df" 2>/dev/null || echo 0)
        n=$((n + 1))
        echo "$n" > "$STUB_LOG.df"
        if [ "$n" -eq 1 ]; then echo "Avail"; echo "$STUB_CONTAINER_FREE"
        else echo "Avail"; echo "${STUB_CONTAINER_FREE_LATER:-$STUB_CONTAINER_FREE}"; fi ;;
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

STUB_DF = r'''#!/usr/bin/env bash
echo "df $*" >> "$STUB_LOG"
path="${@: -1}"
case "$path" in
  *stub-volume*) value="$STUB_HOST_FREE" ;;
  *stub-docker-root*|*/var/lib/docker*) value="$STUB_DOCKER_FREE" ;;
  *) value="" ;;
esac
echo "Avail"
echo "$value"
'''

STUB_GIT = r'''#!/usr/bin/env bash
echo "git $*" >> "$STUB_LOG"
case "$*" in
  *rev-parse*) echo "$DRYRUN_REVISION" ;;
  *archive*) tar -C "$STUB_ARCHIVE" -cf - . ;;
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


# A healthy host. The selection is 5 GiB, so the data volume needs 2 x 5 + 3 = 13 GiB, and the Docker root 6 GiB.
HEALTHY = {
    'STUB_DB_SIZE': str(120 * GIB),
    'STUB_CONTAINER_FREE': str(40 * GIB),
    'STUB_HOST_FREE': str(40 * GIB),
    'STUB_DOCKER_FREE': str(40 * GIB),
    'STUB_VOLUME_MOUNT': '/srv/stub-volume/_data',
    'STUB_DOCKER_ROOT': '/srv/stub-docker-root',
    'STUB_COUNT': '1234',
}


class VmRun:
    def __init__(self, returncode, output, calls, state_created, leftovers):
        self.returncode = returncode
        self.output = output
        self.calls = calls
        self.state_created = state_created
        self.leftovers = leftovers


def run_vm_program(migrations=FIXTURE_MIGRATIONS, inventory=None, **overrides):
    """Run the real VM program with stub docker, df, git, timeout and python3 on PATH."""
    bash = find_bash()
    if bash is None:
        raise unittest.SkipTest('no bash available to run the VM program')
    values = dict(HEALTHY)
    values.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        stubs = tmp / 'bin'
        stubs.mkdir()
        archive = write_fixture_tree(tmp / 'archive', migrations)
        inventory_file = tmp / 'inventory.tsv'
        inventory_file.write_text(inventory if inventory is not None else inventory_text(), encoding='utf-8')
        state = tmp / 'state'  # not created here: the program creates it, and removes its work directory
        log = tmp / 'calls.log'
        log.write_text('', encoding='utf-8')
        write_stub(stubs / 'docker', STUB_DOCKER)
        write_stub(stubs / 'df', STUB_DF)
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
            'STUB_ARCHIVE': msys(archive),
            'STUB_INVENTORY': msys(inventory_file),
        }
        env.update(values)
        # Prepend the stubs inside bash, then source the program in that same shell: Git Bash puts its own tool
        # directories ahead of any inherited PATH, including when a second bash starts.
        launch = 'PATH="$STUB_DIR:$PATH"; export PATH; . "$PROGRAM"'
        result = subprocess.run([bash, '-c', launch], env=env, capture_output=True, text=True, timeout=300)
        calls = log.read_text(encoding='utf-8')
        state_created = state.exists()
        leftovers = list(state.glob('run.*')) if state_created else []
    return VmRun(result.returncode, result.stdout + result.stderr, calls, state_created, leftovers)


class SelectiveCopyTests(unittest.TestCase):
    def test_the_whole_schema_is_copied_and_only_the_selected_rows_are(self):
        run = run_vm_program()
        self.assertIn('pg_dump -U user -d aegra -s -Fc --no-owner --no-acl', run.calls)
        data_dump = [line for line in run.calls.splitlines() if 'pg_dump -U user -d aegra -a' in line]
        self.assertEqual(len(data_dump), 1, run.calls)
        for table in ('account_devices', 'account_device_owner', 'link_account_chain',
                      'phone_push_subscription', 'link_state_admission', 'alembic_version'):
            self.assertIn(f'-t public.{table}', data_dump[0])
        for table in ('phone_chat_event', 'relay_events', 'runs'):
            self.assertNotIn(f'-t public.{table}', data_dump[0])
        self.assertIn('-a --disable-triggers --no-owner --no-acl --exit-on-error', run.calls)

    def test_the_schema_only_table_is_reported_with_its_production_rows_and_size(self):
        run = run_vm_program()
        self.assertIn('phone_chat_event: production rows 1234; total size 50.00 GiB', run.output)
        self.assertIn('schema-only tables: phone_chat_event', run.output)

    def test_a_count_that_cannot_be_read_is_reported_as_not_read_never_as_zero(self):
        run = run_vm_program(STUB_COUNT='')
        self.assertIn('phone_chat_event: production rows not read', run.output)
        self.assertNotIn('phone_chat_event: production rows 0', run.output)

    def test_every_production_read_is_read_only_and_no_write_reaches_production(self):
        run = run_vm_program()
        production = [line for line in run.calls.splitlines()
                      if '-d aegra' in line and 'pg_dump' not in line]
        self.assertTrue(production, run.calls)
        for line in production:
            self.assertIn('SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY', line)
            self.assertIsNone(re.search(r'INSERT INTO|UPDATE |DELETE FROM|ALTER TABLE|DROP TABLE|CREATE TABLE|TRUNCATE',
                                        line), line)

    def test_the_selection_is_refused_above_the_ceiling_before_anything_is_created(self):
        rows = [(name, 25 * GIB if name == 'account_devices' else size, est)
                for name, size, est in FIXTURE_INVENTORY]
        run = run_vm_program(inventory=inventory_text(rows))
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn('above the 20.00 GiB ceiling', run.output)
        self.assertNotIn('CREATE DATABASE', run.calls)
        self.assertNotIn('docker build', run.calls)
        self.assertNotIn('pg_dump', run.calls)
        self.assertEqual(run.leftovers, [], 'the work directory must be removed')

    def test_a_derivation_that_fails_refuses_the_run_before_anything_is_copied(self):
        migrations = dict(FIXTURE_MIGRATIONS)
        migrations['20261126120000_phone_simple.py'] = migration(
            'x20261126_phone_simple', 'w20261031_usage_turn_ts', 'DDL = [f"ALTER TABLE {name} ADD COLUMN x TEXT"]')
        run = run_vm_program(migrations=migrations)
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn('could not be derived', run.output)
        self.assertNotIn('CREATE DATABASE', run.calls)
        self.assertNotIn('docker build', run.calls)


class DiskGuardTests(unittest.TestCase):
    """The copy holds the selected data again on the same volume, and the build writes under the Docker root."""

    def assert_refused_before_anything_was_created(self, run, expected):
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn('refusing (before anything is copied or built)', run.output)
        self.assertIn(expected, run.output)
        self.assertNotIn('CREATE DATABASE', run.calls)
        self.assertNotIn('docker build', run.calls)
        self.assertNotIn('pg_dump', run.calls)
        self.assertEqual(run.leftovers, [], 'the work directory must be removed')

    def test_refuses_when_the_data_volume_is_short_in_the_container_and_creates_nothing(self):
        # 2 x 5 GiB + 3 GiB = 13 GiB needed; the container reports 12 GiB free.
        run = run_vm_program(STUB_CONTAINER_FREE=str(12 * GIB))
        self.assert_refused_before_anything_was_created(run, 'in the container 12.00 GiB')
        self.assertIn('data volume needs 13.00 GiB', run.output)
        self.assertIn('selected data 5368717312 bytes (5.00 GiB)', run.output)

    def test_refuses_when_the_data_volume_is_short_on_the_vm_even_if_the_container_agrees(self):
        run = run_vm_program(STUB_HOST_FREE=str(12 * GIB))
        self.assert_refused_before_anything_was_created(run, 'on the VM 12.00 GiB')

    def test_refuses_when_the_docker_root_has_less_than_6_GiB_free(self):
        run = run_vm_program(STUB_DOCKER_FREE=str(5 * GIB))
        self.assert_refused_before_anything_was_created(run, 'Docker root below 6 GiB')
        self.assertIn('under the Docker root 5.00 GiB', run.output)

    def test_refuses_rather_than_guessing_when_a_measurement_is_not_a_number(self):
        run = run_vm_program(STUB_DB_SIZE='not-a-number')
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn('could not read the production database size', run.output)
        self.assertNotIn('CREATE DATABASE', run.calls)

    def test_the_recheck_before_the_restore_drops_the_copy_and_never_restores(self):
        # The first check (before anything) passes with 40 GiB; the recheck after the copy is created sees 12 GiB.
        run = run_vm_program(STUB_CONTAINER_FREE_LATER=str(12 * GIB))
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn('refusing (before the restore)', run.output)
        self.assertIn('the copy is dropped', run.output)
        self.assertLess(run.calls.index('docker build'), run.calls.index('CREATE DATABASE'))
        self.assertLess(run.calls.index('CREATE DATABASE'), run.calls.index('DROP DATABASE'))
        self.assertNotIn('pg_restore', run.calls)
        self.assertNotIn('pg_dump', run.calls)
        self.assertEqual(run.leftovers, [])

    def test_with_space_to_spare_both_checks_pass_and_the_copy_is_restored(self):
        run = run_vm_program()
        self.assertNotIn('refusing', run.output)
        self.assertIn('disk check (before anything is copied or built)', run.output)
        self.assertIn('disk check (before the restore)', run.output)
        self.assertIn('pg_restore', run.calls)
        # The stub migration fails on purpose, so the run ends there, after the copy was dropped.
        self.assertIn('alembic upgrade head', run.calls)
        self.assertIn('DROP DATABASE', run.calls)


class PhaseTimingTests(unittest.TestCase):
    def test_each_phase_prints_its_time(self):
        run = run_vm_program()
        for phase in ('phase fetch:', 'phase production inventory:', 'phase schema copy',
                      'phase data copy', 'phase drop:'):
            self.assertIn(phase, run.output)

    def test_a_failed_migration_still_prints_the_drop_time(self):
        run = run_vm_program()
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn('phase drop:', run.output)


class FailedMigrationStillDropsTheCopyTests(unittest.TestCase):
    def test_a_failed_migration_drops_the_copy_and_keeps_the_login_out_of_every_output(self):
        run = run_vm_program()
        self.assertNotEqual(run.returncode, 0, run.output)
        created = re.search(r'CREATE DATABASE \\?"?(relay_migration_dryrun_\d{14})', run.calls)
        self.assertIsNotNone(created, run.calls + run.output)
        copy = created.group(1)
        self.assertIn(f'DROP DATABASE IF EXISTS "{copy}"', run.calls)
        self.assertLess(run.calls.index('alembic upgrade head'), run.calls.index('DROP DATABASE'))
        self.assertIn('docker rm -f relay-dryrun-alembic', run.calls)
        self.assertIn(f'docker image rm -f relay-migration-dryrun:{REVISION[:12]}', run.calls)
        self.assertEqual(run.leftovers, [], 'the work directory must be removed')
        combined = run.output + run.calls
        self.assertNotIn(TOKEN, combined)
        self.assertNotIn('stub-password', combined)


PROBE_BOOTSTRAP = (
    'GRAPHS_DIR = os.path.join(os.getcwd(), "graphs")\n'
    'if GRAPHS_DIR not in sys.path:\n'
    '    sys.path.insert(0, GRAPHS_DIR)\n'
)


def probe_program():
    match = re.search(r"<<'PROBE'\n(.*?)\nPROBE\n", PROGRAM.read_text(encoding='utf-8'), re.S)
    assert match, 'the PROBE program is missing'
    return match.group(1) + '\n'


def build_image_layout(root):
    """The image's layout: /app holds alembic/ and graphs/ (react_agent lives there), and site-packages holds
    psycopg, alembic and sqlalchemy but NOT react_agent, because the wheel lists only src/agent_server."""
    app = root / 'app'
    versions = app / 'alembic' / 'versions'
    versions.mkdir(parents=True)
    (versions / '20261126160000_chain_devices_to_account_rows.py').write_text(
        '"""Fixture: the chain import as the image ships it."""\n'
        'from alembic import op\n'
        'from sqlalchemy import text\n\n'
        'revision = "b20261126_chain_import"\n\n'
        'def _fold(owner, links):\n'
        '    raise NotImplementedError\n', encoding='utf-8', newline='\n')
    package = app / 'graphs' / 'react_agent' / 'link'
    package.mkdir(parents=True)
    (app / 'graphs' / 'react_agent' / '__init__.py').write_text('', encoding='utf-8')
    (package / '__init__.py').write_text('', encoding='utf-8')
    (package / 'account_registry.py').write_text('MAX_REGISTRY_DEVICES = 10\n', encoding='utf-8', newline='\n')
    (package / 'chain.py').write_text(
        'class ChainError(Exception):\n    pass\n\n\n'
        'def fold_chain(*args, **kwargs):\n    raise NotImplementedError\n\n\n'
        'def verify_genesis(*args, **kwargs):\n    raise NotImplementedError\n', encoding='utf-8', newline='\n')
    site = root / 'site-packages'
    (site / 'psycopg').mkdir(parents=True)
    (site / 'psycopg' / '__init__.py').write_text(
        'class ProbeReached(Exception):\n    pass\n\n\n'
        'def connect(*args, **kwargs):\n'
        '    raise ProbeReached("the imports resolved; the probe reached the database boundary")\n',
        encoding='utf-8', newline='\n')
    (site / 'alembic').mkdir()
    (site / 'alembic' / '__init__.py').write_text('op = None\n', encoding='utf-8', newline='\n')
    (site / 'sqlalchemy').mkdir()
    (site / 'sqlalchemy' / '__init__.py').write_text('def text(sql):\n    return sql\n', encoding='utf-8', newline='\n')
    return app, site


def run_probe_as_the_image_runs_it(app, site, source):
    """`python -I -B - before < probe.py` from /app. The only addition is the fake site-packages directory, which
    stands in for the image's installed packages. -I keeps the real semantics: no PYTHONPATH, no script directory,
    no user site, and PYTHONPATH below is set to a path that does not exist to prove it is ignored."""
    wrapper = (f"import sys; sys.path.insert(0, {str(site)!r}); "
               "exec(compile(sys.stdin.read(), 'probe.py', 'exec'), {'__name__': '__main__'})")
    env = {
        'PATH': os.environ.get('PATH', ''),
        'PYTHONPATH': str(app / 'does-not-exist'),
        'RELAY_DRYRUN_DSN': 'postgresql://user:pw@127.0.0.1:1/dryrun',
        'RELAY_DRYRUN_DB': 'dryrun',
        'RELAY_DRYRUN_START': 'w20261031_usage_turn_ts',
        'RELAY_DRYRUN_HEAD': 'c20261126_sign_out_everywhere',
    }
    return subprocess.run([sys.executable, '-I', '-B', '-c', wrapper, 'before'], input=source, cwd=app, env=env,
                          capture_output=True, text=True, timeout=120)


class ProbeImportTests(unittest.TestCase):
    """The probe runs in the image with react_agent reachable only from /app/graphs (the failure of 2026-11-27)."""

    def test_the_probe_is_the_one_that_adds_graphs_before_it_imports_react_agent(self):
        source = probe_program()
        self.assertIn(PROBE_BOOTSTRAP, source)
        self.assertLess(source.index(PROBE_BOOTSTRAP), source.index('from react_agent'))

    def test_under_python_I_from_the_image_layout_the_probe_reaches_the_database_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, site = build_image_layout(Path(tmp))
            result = run_probe_as_the_image_runs_it(app, site, probe_program())
        self.assertNotIn('No module named', result.stderr, result.stderr)
        self.assertIn('ProbeReached', result.stderr, result.stderr)
        self.assertNotEqual(result.returncode, 0)

    def test_control_without_the_bootstrap_fails_the_way_the_real_run_failed(self):
        # The same layout and command, with the bootstrap removed, must reproduce the failure. This is the proof that
        # the layout above reaches the same import error as the image, so the passing test is not vacuous.
        source = probe_program()
        self.assertIn(PROBE_BOOTSTRAP, source)
        with tempfile.TemporaryDirectory() as tmp:
            app, site = build_image_layout(Path(tmp))
            result = run_probe_as_the_image_runs_it(app, site, source.replace(PROBE_BOOTSTRAP, ''))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModuleNotFoundError: No module named 'react_agent'", result.stderr, result.stderr)


if __name__ == '__main__':
    unittest.main()
