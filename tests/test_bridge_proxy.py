from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_bridge_proxy import add_include, INCLUDE
import install_bridge_proxy


class BridgeProxyTests(unittest.TestCase):
    SITE = 'server {\n server_name existing.example.com;\n location / { proxy_pass http://existing; }\n listen 443 ssl; # managed by Certbot\n}\n'

    def test_existing_backend_and_tls_are_preserved_and_install_is_idempotent(self):
        updated = add_include(self.SITE, 'existing.example.com')
        self.assertEqual(updated.replace(' ' + INCLUDE + '\n', ''), self.SITE)
        self.assertEqual(add_include(updated, 'existing.example.com'), updated)

    def test_ambiguous_listener_and_different_hostname_fail_closed(self):
        for source, host in [(self.SITE * 2, 'existing.example.com'),
                             (self.SITE, 'other.example.com'), (self.SITE, 'host;bad')]:
            with self.assertRaises(ValueError):
                add_include(source, host)

    def test_failed_nginx_validation_restores_original_before_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def mapped(value):
                return root / value.lstrip('/')
            target = mapped('/etc/nginx/sites-enabled/existing')
            target.parent.mkdir(parents=True)
            target.write_text(self.SITE)
            snippet = mapped('/etc/nginx/snippets/companion-builder-bridge.conf')
            snippet.parent.mkdir(parents=True)
            mapped('/var/backups').mkdir(parents=True)
            commands = []
            def run(args, **kwargs):
                commands.append(args)
                if len(commands) == 1:
                    raise subprocess.CalledProcessError(1, args)
                return subprocess.CompletedProcess(args, 0)
            with patch.object(install_bridge_proxy, 'Path', mapped), \
                 patch.object(install_bridge_proxy.subprocess, 'run', run):
                with self.assertRaises(subprocess.CalledProcessError):
                    install_bridge_proxy.install(dict(BRIDGE_NGINX_SITE='/etc/nginx/sites-enabled/existing',
                                                     BRIDGE_PUBLIC_HOST='existing.example.com'))
            self.assertEqual(target.read_text(), self.SITE)
            self.assertFalse(snippet.exists())
            self.assertEqual(len(list(mapped('/var/backups/companion-builder-bridge').glob('*.conf'))), 1)
            self.assertEqual(commands, [['nginx', '-t'], ['nginx', '-t'], ['systemctl', 'reload', 'nginx']])
