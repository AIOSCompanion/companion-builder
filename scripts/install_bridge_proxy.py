"""Add dedicated routes to the existing TLS server, with validation and rollback."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

MARKER = '# Companion public builder routes v1'
INCLUDE = 'include /etc/nginx/snippets/companion-builder-bridge.conf; # public-builder'
ROUTES = MARKER + '''
location = /builder/webhook {
    client_max_body_size 25m;
    client_body_timeout 30s;
    access_log off;
    proxy_pass http://127.0.0.1:8123/webhook;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_read_timeout 30s;
}
location ^~ /builder/delivery/ {
    access_log off;
    proxy_pass http://127.0.0.1:8123/delivery/;
    proxy_http_version 1.1;
    proxy_set_header Authorization $http_authorization;
    proxy_read_timeout 30s;
}
'''


def add_include(source, host):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*', host):
        raise ValueError('Invalid TLS hostname')
    anchors = list(re.finditer(r'(?m)^([ \t]*)listen 443 ssl;[^\n]*$', source))
    if len(anchors) != 1:
        raise ValueError('Expected exactly one existing TLS listener')
    anchor = anchors[0]
    start = source.rfind('server {', 0, anchor.start())
    if start < 0 or not re.search(r'\bserver_name\s+' + re.escape(host) + r'\s*;', source[start:anchor.start()]):
        raise ValueError('Existing TLS server does not match the configured hostname')
    if INCLUDE in source:
        if source.count(INCLUDE) != 1 or INCLUDE not in source[start:anchor.start()]:
            raise ValueError('Unexpected existing bridge include')
        return source
    return source[:anchor.start()] + anchor.group(1) + INCLUDE + '\n' + source[anchor.start():]


def atomic_write(path, data, mode=0o644):
    descriptor, name = tempfile.mkstemp(prefix='.bridge-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(config):
    site = config['BRIDGE_NGINX_SITE']
    if not re.fullmatch(r'/etc/nginx/sites-enabled/[A-Za-z0-9._-]+', site):
        raise ValueError('Unexpected nginx site path')
    target = Path(site).resolve(strict=True)
    if not target.is_relative_to(Path('/etc/nginx')) or not target.is_file():
        raise ValueError('Nginx target is outside its expected directory')
    original = target.read_bytes()
    updated = add_include(original.decode(), config['BRIDGE_PUBLIC_HOST']).encode()
    snippet = Path('/etc/nginx/snippets/companion-builder-bridge.conf')
    previous = snippet.read_bytes() if snippet.exists() else None
    if previous is not None and not previous.startswith(MARKER.encode()):
        raise ValueError('Existing snippet is not owned by this installer')
    # Keep root's configuration backups outside the receiver's writable state.
    backups = Path('/var/backups/companion-builder-bridge')
    backups.mkdir(mode=0o700, exist_ok=True)
    backups.chmod(0o700)
    backup = backups / (hashlib.sha256(original).hexdigest() + '.conf')
    if not backup.exists():
        atomic_write(backup, original, 0o600)
    mode = target.stat().st_mode & 0o777
    try:
        atomic_write(snippet, ROUTES.encode())
        atomic_write(target, updated, mode)
        subprocess.run(['nginx', '-t'], check=True, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True, timeout=30)
    except Exception:
        atomic_write(target, original, mode)
        if previous is None:
            snippet.unlink(missing_ok=True)
        else:
            atomic_write(snippet, previous)
        subprocess.run(['nginx', '-t'], check=True, capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True, capture_output=True, timeout=30)
        raise
    print('Dedicated bridge routes installed; nginx validation and reload passed')


if __name__ == '__main__':
    install(json.load(sys.stdin))
