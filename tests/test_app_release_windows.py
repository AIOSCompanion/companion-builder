import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import app_release_windows
except (ImportError, ValueError):  # ctypes.wintypes may not import off Windows
    app_release_windows = None


@unittest.skipIf(app_release_windows is None, "the Windows probe module imports Windows-only ctypes types")
class WindowsProbeTests(unittest.TestCase):
    def test_a_locked_webview2_folder_cannot_fail_checks_that_already_passed(self):
        # Merge gate F5 (17 Nov 2026): WebView2's helper processes can hold files in the probe's temporary home for
        # a moment after the window closes; removing that folder must not turn passed checks into a failure.
        seen = {}

        class Stop(Exception):
            pass

        def temporary(**kwargs):
            seen.update(kwargs)
            raise Stop

        with patch.object(app_release_windows.os, "name", "nt"), \
                patch.object(app_release_windows.tempfile, "TemporaryDirectory", side_effect=temporary):
            with self.assertRaises(Stop):
                app_release_windows.check(tempfile.gettempdir(), "v1.2.3")
        self.assertIs(seen.get("ignore_cleanup_errors"), True)
        self.assertEqual(seen.get("prefix"), "companion-package-")


if __name__ == "__main__":
    unittest.main()
