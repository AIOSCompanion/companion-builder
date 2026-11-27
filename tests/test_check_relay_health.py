"""The post-deploy relay check must use the controller's healthy shape and fail closed."""
from pathlib import Path
import socket
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

import check_relay_health as relay  # noqa: E402

HEALTHY = {'status': 'healthy', 'database': 'connected',
           'langgraph_checkpointer': 'connected', 'langgraph_store': 'connected'}


class RelayHealthTests(unittest.TestCase):
    def test_only_the_controller_healthy_shape_passes(self):
        self.assertTrue(relay.is_healthy(HEALTHY))
        for key, value in (('status', 'degraded'), ('database', 'busy: probe budget exceeded'),
                           ('langgraph_checkpointer', 'unknown'), ('langgraph_store', 'connected ')):
            self.assertFalse(relay.is_healthy({**HEALTHY, key: value}), key)
        self.assertFalse(relay.is_healthy({k: v for k, v in HEALTHY.items() if k != 'database'}))
        self.assertFalse(relay.is_healthy(['healthy']))

    def test_a_degraded_200_is_retried_and_never_reported_healthy(self):
        calls = []

        def fetcher(url, timeout):
            calls.append(url)
            return {**HEALTHY, 'database': 'unknown'}

        self.assertFalse(relay.check('https://relay.invalid/health', 3, 0, fetcher=fetcher, sleep=lambda s: None))
        self.assertEqual(len(calls), 3)

    def test_a_healthy_relay_passes_on_the_first_attempt(self):
        calls = []
        self.assertTrue(relay.check('https://relay.invalid/health', 3, 0,
                                    fetcher=lambda url, timeout: calls.append(url) or HEALTHY,
                                    sleep=lambda s: None))
        self.assertEqual(len(calls), 1)

    def test_a_real_closed_port_is_reported_unreachable_not_healthy(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        self.assertFalse(relay.check(f'http://127.0.0.1:{port}/health', 1, 0, timeout=2.0, sleep=lambda s: None))

    def test_the_cli_refuses_plain_http_and_unbounded_attempts(self):
        with self.assertRaises(SystemExit):
            relay.main(['--url', 'http://relay.invalid/health'])
        with self.assertRaises(SystemExit):
            relay.main(['--url', 'https://relay.invalid/health', '--attempts', '0'])


if __name__ == '__main__':
    unittest.main()
