from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from cloud_runtime_inventory import REMOTE


class RuntimeInventoryTests(unittest.TestCase):
    def test_report_omits_environment_values_and_operation_secrets_and_only_runs_reads(self):
        container = {'Id': 'a' * 64, 'Name': '/existing', 'Config': {'Image': 'existing:sha',
            'Env': ['CLOUDFLARE_AUTH_TOKEN=synthetic-provider-secret', 'ORDINARY=private-value'], 'Labels': None},
            'State': {'Status': 'running', 'StartedAt': 'absolute-time'},
            'HostConfig': {'RestartPolicy': {'Name': 'unless-stopped'}}, 'Mounts': []}
        def run(args, **kwargs):
            if args == ['docker', 'ps', '-aq']:
                output = 'a' * 64 + '\n'
            elif args == ['docker', 'inspect', 'a' * 64]:
                output = json.dumps([container])
            elif args[:2] == ['systemctl', 'is-active']:
                output = 'active\n'
            else:
                self.fail('Unexpected or mutating remote command: ' + repr(args))
            return subprocess.CompletedProcess(args, 0, stdout=output)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'var/lib/companion-deploy'
            state.mkdir(parents=True)
            (state / 'current.json').write_text(json.dumps({'phase': 'active', 'token': 'synthetic-operation-secret'}))
            fake_paths = types.SimpleNamespace(Path=lambda value: root / value.lstrip('/'))
            output = io.StringIO()
            with patch.dict(sys.modules, {'pathlib': fake_paths}), \
                 patch.object(subprocess, 'run', run), redirect_stdout(output):
                exec(compile(REMOTE, '<trusted-remote-inventory>', 'exec'), {})
            text = output.getvalue()
            for secret in ('synthetic-provider-secret', 'private-value', 'synthetic-operation-secret'):
                self.assertNotIn(secret, text)
            report = json.loads(text)
            self.assertEqual(report['containers'][0]['retired_provider_environment_fields'], ['CLOUDFLARE_AUTH_TOKEN'])
            self.assertEqual(report['deployment_state'][0]['files'][0]['state'], {'phase': 'active'})
