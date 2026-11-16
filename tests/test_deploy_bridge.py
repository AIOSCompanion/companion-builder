from pathlib import Path
import json
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import deploy_bridge


class BridgeDeploymentTests(unittest.TestCase):
    def config(self):
        env = {name: 'synthetic-' + name for name in deploy_bridge.REQUIRED}
        env.update(BRIDGE_APP_ID='12345', BRIDGE_ENABLED='true',
                   BRIDGE_SSH_HOST='existing.example.com', BRIDGE_SSH_USER='deployer')
        return env

    def test_missing_configuration_prevents_deployment(self):
        with self.assertRaises(ValueError):
            deploy_bridge.deploy({}, run=lambda *a, **kw: self.fail('deployed without configuration'))

    def test_ssh_is_host_verified_and_secrets_only_pass_through_stdin(self):
        env, calls = self.config(), []
        def run(args, **kwargs):
            if args[0] == 'ssh':
                self.assertIn('StrictHostKeyChecking=yes', args)
                self.assertIn('BatchMode=yes', args)
                key = Path(args[args.index('-i') + 1])
                self.assertEqual(key.read_text(), env['BRIDGE_SSH_PRIVATE_KEY'])
                self.assertEqual(json.loads(kwargs['input'])['config']['BRIDGE_APP_PRIVATE_KEY'], env['BRIDGE_APP_PRIVATE_KEY'])
                for name in deploy_bridge.REQUIRED:
                    if name not in ('BRIDGE_SSH_HOST', 'BRIDGE_SSH_USER'):
                        self.assertNotIn(env[name], ' '.join(args))
                self.assertFalse(any(name in kwargs['env'] for name in deploy_bridge.REQUIRED))
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0)
        deploy_bridge.deploy(env, run)
        self.assertEqual([args[0] for args, _ in calls], ['node', 'ssh'])

    def test_failed_app_scope_audit_never_connects_to_the_host(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 1)
        with self.assertRaises(RuntimeError):
            deploy_bridge.deploy(self.config(), run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'node')

    def test_target_cannot_inject_shell_options_or_commands(self):
        for target in ('-oProxyCommand=bad', 'host;bad', 'host$(bad)', 'host\nother'):
            env = self.config()
            env['BRIDGE_SSH_HOST'] = target
            with self.assertRaises(ValueError):
                deploy_bridge.deploy(env, run=lambda *a, **k: self.fail('unsafe target'))

    def test_remote_failure_does_not_echo_output(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 1 if args[0] == 'ssh' else 0,
                                               stdout='synthetic-sensitive-output')
        with self.assertRaisesRegex(RuntimeError, 'remote output withheld'):
            deploy_bridge.deploy(self.config(), run)
