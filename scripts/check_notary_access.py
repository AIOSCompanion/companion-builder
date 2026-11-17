"""Read-only Apple-native check; run only inside the sealed-output wrapper."""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid


def check(env, run=subprocess.run):
    key = env.get('APPLE_API_KEY', '')
    issuer = env.get('APPLE_API_ISSUER', '')
    key_id = env.get('APPLE_API_KEY_ID', '')
    uuid.UUID(issuer)
    if not re.fullmatch(r'[A-Z0-9]{10}', key_id) or not key.startswith('-----BEGIN PRIVATE KEY-----'):
        raise ValueError('Missing or invalid notarization configuration')
    with tempfile.TemporaryDirectory(prefix='builder-notary-') as temporary:
        folder = Path(temporary)
        folder.chmod(0o700)
        path = folder / ('AuthKey_' + key_id + '.p8')
        path.write_text(key)
        path.chmod(0o600)
        # Native output flows only into the caller's encrypted log pipe. No
        # submission is made and no signing certificate/password is needed.
        return run(['xcrun', 'notarytool', 'history', '--key', str(path),
                    '--key-id', key_id, '--issuer', issuer, '--output-format', 'json'],
                   timeout=120, check=False).returncode


if __name__ == '__main__':
    try:
        sys.exit(check(dict(os.environ)))
    except Exception:
        print('Apple native access probe failed; configuration values withheld', file=sys.stderr)
        sys.exit(1)
