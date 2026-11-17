"""Host inspection must preserve the approved main-only cloud boundary."""
from pathlib import Path
import unittest

import yaml


class HostAccessTests(unittest.TestCase):
    def test_cloud_identity_is_available_only_in_approved_main_job(self):
        source = (Path(__file__).resolve().parents[1] / '.github/workflows/host-access.yml').read_text()
        workflow = yaml.safe_load(source)
        self.assertEqual(workflow.get('on', workflow.get(True)), {'workflow_dispatch': None})
        job = workflow['jobs']['inspect']
        self.assertEqual(job['if'], "github.ref == 'refs/heads/main'")
        self.assertEqual(job['environment'], 'bridge-deploy')
        self.assertEqual(job['permissions'], {'contents': 'read', 'id-token': 'write'})
        self.assertNotIn('PRIVATE_KEY', source)
        self.assertNotIn('credentials_json', source)

    def test_cloud_commands_are_read_only_and_reports_remain_sealed(self):
        source = (Path(__file__).resolve().parents[1] / '.github/workflows/host-access.yml').read_text()
        job = yaml.safe_load(source)['jobs']['inspect']
        commands = [step['run'] for step in job['steps'] if 'gcloud compute' in step.get('run', '')]
        self.assertEqual(len(commands), 2)
        for command, operation in zip(commands, ('describe', 'get-guest-attributes')):
            self.assertIn('scripts/run_sealed.py', command)
            self.assertIn('gcloud compute instances ' + operation, command)
            self.assertNotIn('ssh', command)
        artifact = job['steps'][-1]['with']
        self.assertEqual(artifact['path'], '${{ runner.temp }}/host-reports/*.gpg')
        self.assertEqual(artifact['retention-days'], 1)
