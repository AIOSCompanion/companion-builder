"""Run the public proxy installer through approved, pinned cloud SSH."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from cloud_ssh import access


def main():
    try:
        env = dict(os.environ)
        config = {name: env[name] for name in ('BRIDGE_NGINX_SITE', 'BRIDGE_PUBLIC_HOST')}
        installer = Path(__file__).with_name('install_bridge_proxy.py').read_text()
        with access(env) as login:
            result = subprocess.run(login['args'] + ['sudo -n python3 -c ' + shlex.quote(installer)],
                                    input=json.dumps(config), capture_output=True, text=True, timeout=120)
            if result.returncode:
                raise RuntimeError('Remote proxy installation failed')
        print('Existing TLS proxy configured; verify the public bridge endpoint next')
        return 0
    except Exception:
        print('Proxy configuration failed; diagnostic values withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
