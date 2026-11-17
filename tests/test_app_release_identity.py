import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_identity import FORBIDDEN_IDS, FORBIDDEN_TARGETS, identity, validate_destination


class AppReleaseIdentityTests(unittest.TestCase):
    TARGET = "AIOSCompanion/approved-app-downloads"

    def metadata(self):
        return dict(id=123456, full_name=self.TARGET, private=False, visibility="public",
                    archived=False, disabled=False, owner={"login": "AIOSCompanion"})

    def test_private_source_tag_is_pinned_only_after_public_destination_is_verified(self):
        seen = []
        def get(path):
            seen.append(path)
            if path == "repos/" + self.TARGET:
                return self.metadata()
            self.assertEqual(path, "repos/AIOSCompanion/companion-engine/git/ref/tags/v1.2.3")
            return {"object": {"type": "commit", "sha": "a" * 40}}
        result = identity("v1.2.3", "a" * 40, self.TARGET, "123456", get)
        self.assertEqual(result["commit_sha"], "a" * 40)
        self.assertEqual(len(seen), 2)

    def test_missing_configuration_legacy_feed_and_source_repositories_fail_before_api_access(self):
        for target, id in [("", ""), (self.TARGET, ""), (self.TARGET, "not-an-id"),
                           ("OtherOrg/downloads", "123456"), *[(v, "123456") for v in FORBIDDEN_TARGETS],
                           *[(self.TARGET, v) for v in FORBIDDEN_IDS]]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_destination(target, id, lambda _: self.fail("unapproved destination reached GitHub"))

    def test_visibility_identity_and_archived_changes_are_rejected(self):
        for key, value in [("id", 999), ("full_name", "AIOSCompanion/renamed"),
                           ("private", True), ("visibility", "private"), ("archived", True), ("disabled", True),
                           ("owner", {"login": "OtherOrg"})]:
            metadata = self.metadata()
            metadata[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_destination(self.TARGET, "123456", lambda _: metadata)

    def test_moved_or_invalid_tag_never_reaches_private_checkout(self):
        def get(path):
            return self.metadata() if path == "repos/" + self.TARGET else {
                "object": {"type": "commit", "sha": "b" * 40}}
        for tag, expected in [("main", ""), ("v1.2.3", "a" * 40), ("v1.2.3;bad", "")]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                identity(tag, expected, self.TARGET, "123456", get)
