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

    def test_metadata_and_pinned_login_reports_remain_sealed(self):
        source = (Path(__file__).resolve().parents[1] / '.github/workflows/host-access.yml').read_text()
        job = yaml.safe_load(source)['jobs']['inspect']
        commands = [step['run'] for step in job['steps'] if 'scripts/run_sealed.py' in step.get('run', '')]
        self.assertEqual(len(commands), 2)
        for command, operation in zip(commands, ('gcloud compute instances describe', 'scripts/cloud_ssh.py')):
            self.assertIn('scripts/run_sealed.py', command)
            self.assertIn(operation, command)
        artifact = job['steps'][-1]['with']
        self.assertEqual(artifact['path'], '${{ runner.temp }}/host-reports/*.gpg')
        self.assertEqual(artifact['retention-days'], 1)
