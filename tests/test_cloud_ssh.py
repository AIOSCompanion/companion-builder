import json
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cloud_ssh


class CloudSSHTests(unittest.TestCase):
    def config(self):
        return dict(BRIDGE_GCP_PROJECT='example-project', BRIDGE_SSH_HOST='existing.example.com',
                    GCP_DEPLOY_SERVICE_ACCOUNT='existing-deployer@example-project.iam.gserviceaccount.com',
                    BRIDGE_SSH_KNOWN_HOSTS='existing.example.com ssh-ed25519 SYNTHETIC')

    def runner(self, calls, *, registration_failure=False):
        def run(args, **kwargs):
            calls.append(args)
            output = ''
            if args[0] == 'ssh-keygen':
                key = Path(args[args.index('-f') + 1])
                key.write_text('synthetic-private-key')
                key.with_suffix('.pub').write_text('synthetic-public-key')
            if 'add' in args:
                if registration_failure:
                    return subprocess.CompletedProcess(args, 1, stdout='sensitive diagnostic')
                output = json.dumps({'loginProfile': {'posixAccounts': [dict(
                    accountId='example-project', operatingSystemType='LINUX', username='sa_123')]}})
            return subprocess.CompletedProcess(args, 0, stdout=output)
        return run

    def test_pinned_host_and_expiring_key_cleanup_even_when_deploy_fails(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, 'simulated deployment failure'):
            with cloud_ssh.access(self.config(), self.runner(calls)) as login:
                args = login['args']
                self.assertIn('StrictHostKeyChecking=yes', args)
                self.assertIn('IdentitiesOnly=yes', args)
                self.assertNotIn('synthetic-private-key', ' '.join(args))
                temporary_key = Path(args[args.index('-i') + 1])
                hosts = Path(next(v.split('=', 1)[1] for v in args if v.startswith('UserKnownHostsFile=')))
                self.assertEqual(hosts.read_text(), self.config()['BRIDGE_SSH_KNOWN_HOSTS'])
                self.assertEqual(args[-1], 'sa_123@existing.example.com')
                raise RuntimeError('simulated deployment failure')
        self.assertFalse(temporary_key.exists())
        self.assertIn('--ttl=15m', calls[1])
        for call in calls[1:]:
            self.assertIn('--account=existing-deployer@example-project.iam.gserviceaccount.com', call)
        self.assertIn('remove', calls[-1])

    def test_registration_failure_never_yields_host_access_or_sensitive_output(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, '^Temporary OS Login registration failed; output withheld$'):
            with cloud_ssh.access(self.config(), self.runner(calls, registration_failure=True)):
                self.fail('unregistered key yielded access')
        self.assertEqual(len(calls), 2)

    def test_missing_pins_and_unsafe_host_fail_before_credentials_are_created(self):
        for field, value in [('BRIDGE_SSH_KNOWN_HOSTS', ''), ('BRIDGE_SSH_HOST', '-oProxyCommand=bad'),
                             ('BRIDGE_GCP_PROJECT', 'project;bad'),
                             ('GCP_DEPLOY_SERVICE_ACCOUNT', 'other@untrusted.iam.gserviceaccount.com')]:
            env = self.config()
            env[field] = value
            with self.assertRaises(ValueError):
                with cloud_ssh.access(env, lambda *a, **k: self.fail('unsafe configuration used')):
                    self.fail('unsafe host yielded')
