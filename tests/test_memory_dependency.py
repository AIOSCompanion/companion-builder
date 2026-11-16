import sys
from pathlib import Path
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from resolve_memory_dependency import resolve
from builder import dispatch_args


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

    def test_engine_peer_is_pinned_the_same_way(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout="c" * 40)
        self.assertEqual(resolve("", run, repository="aiosxaegra"), "c" * 40)
        self.assertEqual(calls[0][2], "repos/AIOSCompanion/aiosxaegra/commits/main")

    def test_unknown_repository_is_refused_before_network(self):
        with self.assertRaises(ValueError):
            resolve("a" * 40, repository="another-private-repo")

    def test_desktop_peer_is_allowed_only_for_engine_tests(self):
        self.assertEqual(resolve('d' * 40, repository='aios-desktop-app'), 'd' * 40)
        args = dispatch_args('test', repository='aiosxaegra', sha='a' * 40, desktop_sha='d' * 40)
        self.assertIn('desktop_sha=' + 'd' * 40, args)
        with self.assertRaises(ValueError):
            dispatch_args('test', repository='companion-memory', sha='a' * 40, desktop_sha='d' * 40)

    def test_engine_contract_checkout_is_immutable_and_receives_no_persistent_token(self):
        import yaml
        workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/test.yml').read_text())
        steps = workflow['jobs']['suite']['steps']
        checkout = next(step for step in steps if step.get('name') == 'Check out the pinned desktop source contracts beside the engine')
        self.assertEqual(checkout['if'], "matrix.repo == 'aiosxaegra'")
        self.assertEqual(checkout['with']['ref'], '${{ steps.desktop-peer.outputs.sha }}')
        self.assertFalse(checkout['with']['persist-credentials'])
        self.assertEqual(workflow['jobs']['suite']['environment'], 'source-access')

    def test_cli_accepts_only_the_matching_pinned_peer(self):
        args = dispatch_args("test", repository="companion-memory", sha="a" * 40, engine_sha="b" * 40)
        self.assertIn("engine_sha=" + "b" * 40, args)
        desktop_args = dispatch_args("test", repository="aios-desktop-app", sha="a" * 40, engine_sha="b" * 40)
        self.assertIn("engine_sha=" + "b" * 40, desktop_args)
        for kwargs in [{"engine_sha": "main"}, {"memory_sha": "b" * 40}]:
            with self.assertRaises(ValueError):
                dispatch_args("test", repository="companion-memory", sha="a" * 40, **kwargs)
