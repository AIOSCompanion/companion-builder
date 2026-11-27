"""The relay and portal deploy routes must keep their approval, main-only and no-leak boundaries.

These tests read the workflow files as they are in this repository. They pin the contract, not the
behaviour of the private deploy itself, which runs only after owner approval.
"""
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / '.github/workflows'
MAIN_ONLY = "github.ref == 'refs/heads/main'"


def load(name):
    source = (WORKFLOWS / name).read_text(encoding='utf-8')
    return source, yaml.safe_load(source)


def trigger(workflow):
    return workflow.get('on', workflow.get(True))


def secrets_referenced(source):
    return set(re.findall(r'secrets\.([A-Z0-9_]+)', source))


class RelayDeployWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source, cls.workflow = load('relay-deploy.yml')
        cls.jobs = cls.workflow['jobs']

    def test_dispatch_only_with_one_exact_sha_input(self):
        self.assertEqual(set(trigger(self.workflow)), {'workflow_dispatch'})
        self.assertEqual(set(trigger(self.workflow)['workflow_dispatch']['inputs']), {'relay_sha'})
        self.assertTrue(trigger(self.workflow)['workflow_dispatch']['inputs']['relay_sha']['required'])
        self.assertIn('^[a-f0-9]{40}$', self.source)

    def test_every_job_is_main_only(self):
        for name, job in self.jobs.items():
            self.assertEqual(job.get('if'), MAIN_ONLY, name)

    def test_private_token_is_minted_only_in_source_access(self):
        source_job = self.jobs['source']
        self.assertEqual(source_job['environment'], 'source-access')
        self.assertNotIn('id-token', source_job.get('permissions', {'contents': 'read'}))
        minting = [s for s in source_job['steps'] if 'create-github-app-token' in s.get('uses', '')]
        self.assertEqual(len(minting), 1)
        self.assertEqual(minting[0]['with']['repositories'], 'aiosxaegra')
        self.assertEqual(minting[0]['with']['permission-contents'], 'read')

    def test_deploy_identity_is_only_in_the_relay_deploy_environment(self):
        deploy = self.jobs['deploy']
        self.assertEqual(deploy['environment'], 'relay-deploy')
        self.assertEqual(deploy['needs'], 'source')
        self.assertEqual(deploy['permissions'], {'contents': 'read', 'id-token': 'write'})
        for name, job in self.jobs.items():
            if name != 'deploy':
                self.assertNotIn('id-token', job.get('permissions', {}), name)

    def test_deploy_target_matches_the_relay_own_deploy(self):
        steps = self.jobs['deploy']['steps']
        vm = [s for s in steps if s.get('uses') == './relay/.github/actions/vm-run']
        self.assertEqual(len(vm), 1)
        self.assertEqual(vm[0]['with']['instance'], 'aiosserver-v2')
        self.assertEqual(vm[0]['with']['zone'], 'europe-north2-b')
        self.assertEqual(vm[0]['with']['project'], 'aios-460415')
        self.assertEqual(vm[0]['with']['script_file'], 'relay/scripts/deploy_remote.sh')

    def test_no_dotenv_is_delivered_and_no_secret_beyond_the_app_key_is_read(self):
        self.assertNotIn('secrets.BACKEND_DOTENV_B64', self.source)
        vm_run = next(s for s in self.jobs['deploy']['steps'] if s.get('uses') == './relay/.github/actions/vm-run')
        self.assertNotIn('BACKEND_DOTENV_B64', vm_run['with']['pass_env'])
        self.assertEqual(secrets_referenced(self.source), {'BUILDER_APP_PRIVATE_KEY'})

    def test_no_artifact_can_carry_private_source(self):
        self.assertNotIn('upload-artifact', self.source)

    def test_the_relay_is_checked_after_the_deploy(self):
        steps = [s.get('name', '') for s in self.jobs['deploy']['steps']]
        self.assertLess(steps.index('Deploy on the VM with the relay\'s vm-run action'),
                        steps.index('Verify the relay is healthy over TLS'))


class PortalDeployWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source, cls.workflow = load('portal-deploy.yml')
        cls.jobs = cls.workflow['jobs']

    def test_dispatch_only_with_exact_sha_and_preview_or_live(self):
        inputs = trigger(self.workflow)['workflow_dispatch']['inputs']
        self.assertEqual(set(trigger(self.workflow)), {'workflow_dispatch'})
        self.assertEqual(set(inputs), {'aios_commercial_sha', 'mode'})
        self.assertEqual(inputs['mode']['options'], ['preview', 'live'])
        self.assertEqual(inputs['mode']['default'], 'preview')

    def test_every_job_is_main_only(self):
        for name, job in self.jobs.items():
            self.assertEqual(job.get('if'), MAIN_ONLY, name)

    def test_source_token_and_deploy_identity_are_separated(self):
        self.assertEqual(self.jobs['source']['environment'], 'source-access')
        self.assertEqual(self.jobs['deploy']['environment'], 'portal-deploy')
        self.assertNotIn('id-token', self.jobs['source'].get('permissions', {}))
        self.assertEqual(self.jobs['deploy']['permissions'], {'contents': 'read', 'id-token': 'write'})

    def test_live_publish_and_verify_run_only_in_live_mode(self):
        steps = {s.get('name'): s for s in self.jobs['deploy']['steps']}
        self.assertEqual(steps['Publish to app.aioscompanion.com']['if'], "inputs.mode == 'live'")
        self.assertEqual(steps['Verify the live site']['if'], "inputs.mode == 'live'")
        self.assertEqual(steps['Deploy to a preview channel']['if'], "inputs.mode == 'preview'")
        self.assertEqual(steps['Verify the preview on real HTTPS']['if'], "inputs.mode == 'preview'")

    def test_firebase_identity_matches_the_portal_workflow(self):
        steps = {s.get('name'): s for s in self.jobs['deploy']['steps']}
        auth = steps['Authenticate to Firebase (keyless)']['with']
        self.assertEqual(auth['workload_identity_provider'],
                         'projects/167541691556/locations/global/workloadIdentityPools/github-actions/providers/aios-commercial')
        self.assertEqual(auth['service_account'], 'portal-hosting-deploy@aios-ce15f.iam.gserviceaccount.com')

    def test_bundle_gates_pin_the_same_contract_as_deploy_portal(self):
        gate = next(s for s in self.jobs['deploy']['steps'] if s.get('name') == 'Gate the bundle')['run']
        for needle in ('apiKey:"AIza', 'appId:"1:', 'functionsUrl:"https://us-central1-aios-ce15f.cloudfunctions.net"',
                       '34-51-149-24', 'http://34\\.51\\.149\\.24', 'companion-wake', 'companion.webmanifest'):
            self.assertIn(needle, gate)

    def test_no_secret_or_artifact_is_used(self):
        self.assertEqual(secrets_referenced(self.source), {'BUILDER_APP_PRIVATE_KEY'})
        self.assertNotIn('upload-artifact', self.source)
        self.assertNotIn('download-artifact', self.source)


if __name__ == '__main__':
    unittest.main()
