"""Deploy the public receiver to an existing SSH host; never print secret values."""
from pathlib import Path
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile

REQUIRED = ('BRIDGE_SSH_HOST', 'BRIDGE_SSH_USER', 'BRIDGE_SSH_PRIVATE_KEY',
            'BRIDGE_SSH_KNOWN_HOSTS', 'BRIDGE_APP_ID', 'BRIDGE_APP_PRIVATE_KEY',
            'BRIDGE_WEBHOOK_SECRET', 'BRIDGE_STATUS_TOKEN')
ROOT = Path(__file__).resolve().parents[1]
FILES = ('worker.mjs', 'server.mjs', 'audit-app.mjs')


def deploy(env, run=subprocess.run):
    missing = [name for name in REQUIRED if not env.get(name)]
    if missing:
        raise ValueError('Missing configuration: ' + ', '.join(missing))
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', env['BRIDGE_SSH_HOST'])
            or not re.fullmatch(r'[a-z_][a-z0-9_-]*', env['BRIDGE_SSH_USER'])
            or not env['BRIDGE_APP_ID'].isdigit()
            or env.get('BRIDGE_ENABLED') not in ('true', 'false')):
        raise ValueError('Invalid SSH target, app ID or enable flag')
    clean_env = {k: v for k, v in env.items() if k not in REQUIRED}
    audit_env = dict(clean_env, **{n: env[n] for n in ('BRIDGE_APP_ID', 'BRIDGE_APP_PRIVATE_KEY')})
    audit = run(['node', str(ROOT / 'bridge/audit-app.mjs')], env=audit_env,
                capture_output=True, text=True, check=False)
    if audit.returncode:
        raise RuntimeError('Dispatch App must access only the public builder; deployment refused')
    payload = {'files': {name: (ROOT / 'bridge' / name).read_text() for name in FILES},
               'config': {name: env[name] for name in ('BRIDGE_APP_ID', 'BRIDGE_APP_PRIVATE_KEY',
                         'BRIDGE_WEBHOOK_SECRET', 'BRIDGE_STATUS_TOKEN', 'BRIDGE_ENABLED')}}
    installer = (ROOT / 'scripts/install_bridge_host.py').read_text()
    with tempfile.TemporaryDirectory(prefix='builder-bridge-') as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        key = folder / 'key'
        key.write_text(env['BRIDGE_SSH_PRIVATE_KEY'])
        key.chmod(0o600)
        hosts = folder / 'known_hosts'
        hosts.write_text(env['BRIDGE_SSH_KNOWN_HOSTS'])
        hosts.chmod(0o600)
        args = ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={hosts}',
                '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=15',
                '-o', 'ServerAliveCountMax=3', f"{env['BRIDGE_SSH_USER']}@{env['BRIDGE_SSH_HOST']}",
                'sudo -n python3 -c ' + shlex.quote(installer)]
        result = run(args, input=json.dumps(payload), env=clean_env,
                     capture_output=True, text=True, check=False, timeout=180)
        if result.returncode:
            raise RuntimeError('Bridge host deployment failed; remote output withheld')


def main():
    try:
        deploy(dict(os.environ))
        print('Bridge host installed; verify TLS webhook delivery before claiming automation live')
        return 0
    except (ValueError, RuntimeError, subprocess.TimeoutExpired):
        print('Bridge deployment failed; check required configuration and host access', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
