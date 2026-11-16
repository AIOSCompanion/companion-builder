"""Trusted remote installer, executed through SSH stdin by the public deploy workflow."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error


def main():
    payload = json.load(sys.stdin)
    files, config = payload['files'], payload['config']
    if set(files) != {'worker.mjs', 'server.mjs', 'audit-app.mjs'}:
        raise ValueError('unexpected public files')
    if config.get('BRIDGE_ENABLED') not in ('true', 'false'):
        raise ValueError('invalid enable flag')
    subprocess.run(['/usr/bin/node', '--input-type=module', '-e',
                    "import {DatabaseSync} from 'node:sqlite'; new DatabaseSync(':memory:').close()"],
                   check=True, capture_output=True)
    user = 'companion-builder-bridge'
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        subprocess.run(['useradd', '--system', '--home-dir', '/var/lib/companion-builder-bridge',
                        '--shell', '/usr/sbin/nologin', user], check=True)
        account = pwd.getpwnam(user)
    root = Path('/opt/companion-builder-bridge')
    root.mkdir(mode=0o755, exist_ok=True)
    digest = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    version = root / digest
    version.mkdir(mode=0o755, exist_ok=True)
    for name, text in files.items():
        (version / name).write_text(text)
        (version / name).chmod(0o644)
    subprocess.run(['systemctl', 'stop', 'companion-builder-bridge.service'], check=False,
                   capture_output=True)
    state = Path('/var/lib/companion-builder-bridge')
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    os.chown(state, account.pw_uid, account.pw_gid)
    config.update(BRIDGE_DB=str(state / 'outbox.sqlite'), BRIDGE_PORT='8123')
    descriptor, temporary_name = tempfile.mkstemp(prefix='.config-', dir=state)
    temporary = Path(temporary_name)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(config, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chown(temporary, account.pw_uid, account.pw_gid)
    os.replace(temporary, state / 'config.json')
    unit = f'''[Unit]
Description=Companion public builder signed event receiver
After=network-online.target
Wants=network-online.target
[Service]
User={user}
Group={account.pw_gid}
Environment=BRIDGE_CONFIG={state}/config.json
ExecStart=/usr/bin/node {version}/server.mjs
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={state}
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
[Install]
WantedBy=multi-user.target
'''
    Path('/etc/systemd/system/companion-builder-bridge.service').write_text(unit)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'companion-builder-bridge.service'], check=True,
                   capture_output=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'companion-builder-bridge.service'], check=True)
    # is-active alone can succeed while the startup scope audit is still running.
    for attempt in range(30):
        try:
            urllib.request.urlopen('http://127.0.0.1:8123/webhook', timeout=2)
        except urllib.error.HTTPError as error:
            if error.code == (503 if config['BRIDGE_ENABLED'] == 'false' else 404):
                return
        except OSError:
            pass
        time.sleep(1)
    raise RuntimeError('receiver failed readiness check')


if __name__ == '__main__':
    main()
