"""Trusted remote installer, executed through SSH stdin by the public deploy workflow."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

NODE_VERSION = '22.23.4'
NODE_ARCHIVE_SHA256 = '1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce'


def bridge_runtime(root):
    """Install an isolated, hash-pinned runtime without replacing system Node."""
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise ValueError('This pinned runtime supports the existing Linux x64 host only')
    runtimes = root / 'runtime'
    runtimes.mkdir(mode=0o755, exist_ok=True)
    name = f'node-v{NODE_VERSION}-linux-x64'
    destination = runtimes / name
    node = destination / 'bin/node'
    if not destination.exists():
        with tempfile.TemporaryDirectory(prefix='.node-', dir=runtimes) as staging:
            stage = Path(staging)
            archive = stage / 'node.tar.xz'
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(f'https://nodejs.org/dist/v{NODE_VERSION}/{name}.tar.xz', timeout=60) as response, archive.open('wb') as output:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > 80 * 1024 * 1024:
                        raise ValueError('Runtime download exceeded its bound')
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != NODE_ARCHIVE_SHA256:
                raise ValueError('Runtime checksum mismatch; no archive extracted')
            subprocess.run(['tar', '-xJf', str(archive), '--no-same-owner', '-C', str(stage)],
                           check=True, capture_output=True, timeout=60)
            extracted = stage / name
            extracted.chmod(0o755)
            os.replace(extracted, destination)
    version = subprocess.run([str(node), '--version'], check=True, capture_output=True, text=True)
    if version.stdout.strip() != 'v' + NODE_VERSION:
        raise ValueError('Unexpected isolated runtime version')
    return node


def main():
    import pwd  # Remote Linux installer; keep runtime validation testable on Windows.
    payload = json.load(sys.stdin)
    files, config = payload['files'], payload['config']
    if set(files) != {'worker.mjs', 'server.mjs', 'audit-app.mjs'}:
        raise ValueError('unexpected public files')
    if config.get('BRIDGE_ENABLED') not in ('true', 'false'):
        raise ValueError('invalid enable flag')
    root = Path('/opt/companion-builder-bridge')
    root.mkdir(mode=0o755, exist_ok=True)
    node = bridge_runtime(root)
    subprocess.run([str(node), '--input-type=module', '-e',
                    "import {DatabaseSync} from 'node:sqlite'; new DatabaseSync(':memory:').close()"],
                   check=True, capture_output=True)
    user = 'companion-builder-bridge'
    try:
        account = pwd.getpwnam(user)
    except KeyError:
        subprocess.run(['useradd', '--system', '--home-dir', '/var/lib/companion-builder-bridge',
                        '--shell', '/usr/sbin/nologin', user], check=True)
        account = pwd.getpwnam(user)
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
ExecStart={node} {version}/server.mjs
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
