import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from resolve_memory_dependency import resolve


class MemoryDependencyTests(unittest.TestCase):
    def test_explicit_pin_does_not_query_main(self):
        self.assertEqual(resolve("a" * 40, lambda *a, **kw: self.fail("network")), "a" * 40)

    def test_rejects_mutable_and_injected_refs(self):
        for value in ["main", "a" * 39, "a" * 40 + "\n", "$(echo bad)"]:
            with self.assertRaises(ValueError):
                resolve(value)

    def test_resolves_only_the_memory_main_commit(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout="b" * 40 + "\n")
        self.assertEqual(resolve("", run), "b" * 40)
        self.assertEqual(calls[0][2], "repos/AIOSCompanion/companion-memory/commits/main")
