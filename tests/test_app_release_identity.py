import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from app_release_identity import FORBIDDEN_IDS, FORBIDDEN_TARGETS, identity, validate_configuration, validate_destination


class AppReleaseIdentityTests(unittest.TestCase):
    TARGET = "AIOSCompanion/approved-app-downloads"

    def metadata(self):
        return dict(id=123456, full_name=self.TARGET, private=False, visibility="public", archived=False,
                    disabled=False, owner={"login": "AIOSCompanion"}, permissions={"push": True})

    def test_the_source_tag_is_pinned_with_the_source_token_and_the_destination_is_never_read_with_it(self):
        seen = []
        def get(path):
            seen.append(path)
            self.assertEqual(path, "repos/AIOSCompanion/companion-engine/git/ref/tags/v1.2.3")
            return {"object": {"type": "commit", "sha": "a" * 40}}
        result = identity("v1.2.3", "a" * 40, self.TARGET, get)
        self.assertEqual(result, {"commit_sha": "a" * 40, "tag_object_sha": "a" * 40,
                                  "release_repository": self.TARGET})
        self.assertEqual(len(seen), 1)

    def test_one_variable_names_the_destination_and_old_or_private_repositories_fail_before_any_access(self):
        targets = ["", "OtherOrg/downloads", "AIOSCompanion/", "AIOSCompanion/x.git", *FORBIDDEN_TARGETS,
                   # GitHub names ignore case: a case variant of today's updater feed is the same repository.
                   "AIOSCompanion/AIOS-Companion-Releases", "aioscompanion/aios-companion-releases"]
        for target in targets:
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_configuration(target)
            with self.subTest(target=target), self.assertRaises(ValueError):
                validate_destination(target, lambda _: self.fail("unapproved destination reached GitHub"))
            with self.subTest(target=target), self.assertRaises(ValueError):
                identity("v1.2.3", "", target, lambda _: self.fail("unapproved destination reached the source tag"))

    def test_todays_updater_feed_is_refused_by_immutable_id_even_under_an_approved_name(self):
        self.assertIn("1025431750", FORBIDDEN_IDS)  # AIOSCompanion/aios-companion-releases
        for forbidden in FORBIDDEN_IDS:
            metadata = dict(self.metadata(), id=int(forbidden))
            with self.subTest(id=forbidden), self.assertRaises(ValueError):
                validate_destination(self.TARGET, lambda _: metadata)

    def test_visibility_identity_archived_and_unwritable_destinations_are_rejected(self):
        for key, value in [("full_name", "AIOSCompanion/renamed"), ("private", True), ("visibility", "private"),
                           ("archived", True), ("disabled", True), ("owner", {"login": "OtherOrg"}),
                           ("permissions", {"push": False})]:
            metadata = self.metadata()
            metadata[key] = value
            with self.subTest(field=key), self.assertRaises(ValueError):
                validate_destination(self.TARGET, lambda _: metadata)
        self.assertEqual(validate_destination(self.TARGET, lambda _: self.metadata())["id"], 123456)

    def test_moved_or_invalid_tag_never_reaches_private_checkout(self):
        get = lambda path: {"object": {"type": "commit", "sha": "b" * 40}}
        for tag, expected in [("main", ""), ("v1.2.3", "a" * 40), ("v1.2.3;bad", "")]:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                identity(tag, expected, self.TARGET, get)


if __name__ == "__main__":
    unittest.main()
