import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from validate_release_ref import resolve
import plan_suites


class ReleaseRefTests(unittest.TestCase):
    def test_direct_workflow_dispatch_cannot_checkout_a_branch_instead_of_a_commit(self):
        self.assertEqual(plan_suites.main(["--repository", "aiosxaegra", "--head-sha", "main"], env={}), 1)

    def test_lightweight_tag_is_pinned_and_webhook_movement_is_refused(self):
        get = lambda _: {"object": {"type": "commit", "sha": "a" * 40}}
        self.assertEqual(resolve("v1.2.3", "a" * 40, get), "a" * 40)
        with self.assertRaises(ValueError):
            resolve("v1.2.3", "b" * 40, get)

    def test_annotated_tag_checks_original_tag_identity_then_peels_to_commit(self):
        seen = []
        def get(path):
            seen.append(path)
            return {"object": {"type": "tag", "sha": "a" * 40}} if "/ref/" in path else {
                "object": {"type": "commit", "sha": "b" * 40}}
        self.assertEqual(resolve("v1.2.3", "a" * 40, get), "b" * 40)
        self.assertEqual(len(seen), 2)

    def test_untrusted_refs_and_noncommit_objects_never_reach_checkout(self):
        for tag in ("main", "v1.2.3/other", "v1.2.3;echo bad"):
            with self.assertRaises(ValueError):
                resolve(tag, "", lambda _: self.fail("invalid ref reached GitHub"))
        with self.assertRaises(ValueError):
            resolve("v1.2.3", "", lambda _: {"object": {"type": "blob", "sha": "a" * 40}})

    def test_cyclic_tag_chain_fails_closed(self):
        with self.assertRaises(ValueError):
            resolve("v1.2.3", "", lambda _: {"object": {"type": "tag", "sha": "a" * 40}})
