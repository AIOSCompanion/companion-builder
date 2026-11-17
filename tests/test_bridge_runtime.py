import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install_bridge_host


class BridgeRuntimeTests(unittest.TestCase):
    def test_checksum_failure_never_extracts_or_executes_download(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(install_bridge_host.platform, 'system', return_value='Linux'), \
             patch.object(install_bridge_host.platform, 'machine', return_value='x86_64'), \
             patch.object(install_bridge_host.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt runtime')), \
             patch.object(install_bridge_host.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                install_bridge_host.bridge_runtime(Path(folder))
            run.assert_not_called()
            self.assertEqual(list((Path(folder) / 'runtime').iterdir()), [])
