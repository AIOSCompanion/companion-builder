"""Deploy through temporary OS Login; never store a reusable SSH private key."""
import os
import sys

from cloud_ssh import access
from deploy_bridge import deploy


def main():
    try:
        env = dict(os.environ)
        with access(env) as login:
            env.update(BRIDGE_SSH_USER=login['username'], BRIDGE_SSH_PRIVATE_KEY=login['private_key'])
            deploy(env)
        print('Bridge installed through temporary, pinned-host OS Login; verify TLS delivery next')
        return 0
    except Exception:
        print('Cloud bridge deployment failed; diagnostic values withheld', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
