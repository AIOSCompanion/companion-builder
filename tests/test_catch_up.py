from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from catch_up import expand, ZERO


class CatchUpTests(unittest.TestCase):
    def test_pagination_includes_all_commits_and_deduplicates_overlapping_pages(self):
        hashes = [f"{n:040x}" for n in range(1, 103)]
        calls, sent = [], []
        def get(path):
            calls.append(path)
            return {"commits": [{"sha": sha} for sha in (hashes[:100] if len(calls) == 1 else hashes[99:])]}
        count = expand("aiosxaegra", "a" * 40, hashes[-1], get, lambda repo, sha: sent.append(sha))
        self.assertEqual(count, 102)
        self.assertEqual(sent, hashes)
        self.assertIn("page=2", calls[-1])

    def test_new_branch_enumerates_reachable_history_and_always_includes_the_head(self):
        head = "b" * 40
        paths, sent = [], []
        def get(path):
            paths.append(path)
            return []
        self.assertEqual(expand("companion-memory", ZERO, head, get, lambda repo, sha: sent.append(sha)), 1)
        self.assertIn("/commits?sha=", paths[0])
        self.assertEqual(sent, [head])

    def test_moving_refs_unknown_repositories_and_malformed_pages_fail_closed(self):
        for repository, before, head in [("other", "a" * 40, "b" * 40),
                                         ("aiosxaegra", "main", "b" * 40),
                                         ("aiosxaegra", ZERO, ZERO)]:
            with self.assertRaises(ValueError):
                expand(repository, before, head, lambda _: self.fail("invalid request reached API"), lambda *a: None)
        with self.assertRaises(ValueError):
            expand("aiosxaegra", "a" * 40, "b" * 40,
                   lambda _: {"commits": [{"sha": "main"}]}, lambda *a: self.fail("invalid SHA dispatched"))
