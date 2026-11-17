"""Temporary OS Login access using the approved job's existing cloud identity."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


@contextmanager
def access(env, run=subprocess.run):
    required = ('BRIDGE_GCP_PROJECT', 'BRIDGE_SSH_HOST', 'BRIDGE_SSH_KNOWN_HOSTS')
    if any(not env.get(name) for name in required):
        raise ValueError('Missing cloud SSH configuration')
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]', env['BRIDGE_GCP_PROJECT']):
        raise ValueError('Invalid project')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', env['BRIDGE_SSH_HOST']):
        raise ValueError('Invalid host')
    with tempfile.TemporaryDirectory(prefix='builder-oslogin-') as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        key = folder / 'key'
        created = run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)],
                      capture_output=True, timeout=15, check=False)
        if created.returncode:
            raise RuntimeError('Could not create temporary SSH key')
        public = key.with_suffix('.pub').read_text().strip()
        registered = False
        try:
            result = run(['gcloud', 'compute', 'os-login', 'ssh-keys', 'add',
                          '--key-file=' + str(key.with_suffix('.pub')), '--ttl=15m',
                          '--project=' + env['BRIDGE_GCP_PROJECT'], '--format=json', '--quiet'],
                         capture_output=True, text=True, timeout=60, check=False)
            if result.returncode:
                raise RuntimeError('Temporary OS Login registration failed; output withheld')
            registered = True
            accounts = json.loads(result.stdout)['loginProfile']['posixAccounts']
            accounts = [a for a in accounts if a.get('accountId') == env['BRIDGE_GCP_PROJECT']
                        and a.get('operatingSystemType') == 'LINUX']
            if len(accounts) != 1 or not re.fullmatch(r'[a-z_][a-z0-9_-]*', accounts[0]['username']):
                raise ValueError('Unexpected OS Login profile')
            hosts = folder / 'known_hosts'
            hosts.write_text(env['BRIDGE_SSH_KNOWN_HOSTS'])
            hosts.chmod(0o600)
            args = ['ssh', '-i', str(key), '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                    '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={hosts}',
                    '-o', 'ConnectTimeout=20', '-o', 'ServerAliveInterval=15',
                    '-o', 'ServerAliveCountMax=3',
                    accounts[0]['username'] + '@' + env['BRIDGE_SSH_HOST']]
            yield {'username': accounts[0]['username'], 'private_key': key.read_text(), 'args': args}
        finally:
            if registered:
                result = run(['gcloud', 'compute', 'os-login', 'ssh-keys', 'remove',
                              '--key=' + public, '--project=' + env['BRIDGE_GCP_PROJECT'], '--quiet'],
                             capture_output=True, timeout=60, check=False)
                if result.returncode:
                    raise RuntimeError('Temporary key cleanup failed; key expires within 15 minutes')


def probe(env):
    with access(env) as login:
        # No application data, environment values or service logs are read.
        result = subprocess.run(login['args'] + ['sudo -n python3 -c ' +
            "\"import json,os,subprocess; print(json.dumps({'sudo_uid':os.getuid(),"
            "'node':subprocess.run(['/usr/bin/node','--version'],capture_output=True,text=True).stdout.strip()}))\""],
            capture_output=True, text=True, timeout=45, check=False)
        if result.returncode:
            raise RuntimeError('Pinned-host SSH probe failed; output withheld')
        report = json.loads(result.stdout)
        if report.get('sudo_uid') != 0:
            raise RuntimeError('Deployment identity lacks administrative host access')
        print(json.dumps(report))


if __name__ == '__main__':
    probe(dict(os.environ))
