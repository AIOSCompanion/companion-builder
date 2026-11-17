from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import check_notary_access


class NotaryAccessTests(unittest.TestCase):
    def test_only_reads_history_and_always_removes_temporary_key_on_failure(self):
        env = dict(APPLE_API_KEY='-----BEGIN PRIVATE KEY-----\nsynthetic\n-----END PRIVATE KEY-----',
                   APPLE_API_KEY_ID='SYNTHETIC1', APPLE_API_ISSUER='00000000-0000-0000-0000-000000000001')
        paths = []
        def run(args, **kwargs):
            self.assertEqual(args[:3], ['xcrun', 'notarytool', 'history'])
            path = Path(args[args.index('--key') + 1])
            self.assertEqual(path.read_text(), env['APPLE_API_KEY'])
            self.assertNotIn(env['APPLE_API_KEY'], ' '.join(args))
            paths.append(path)
            raise subprocess.TimeoutExpired(args, 120)
        with self.assertRaises(subprocess.TimeoutExpired):
            check_notary_access.check(env, run)
        self.assertFalse(paths[0].exists())
