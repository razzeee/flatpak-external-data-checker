import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src import main
from src.lib.prmetadata import MARKER, MetadataError, format_metadata, parse_metadata

from .update_support import UpdateFixture


class TestUpdateMetadata(UpdateFixture):
    def test_metadata_roundtrip_escapes_comment_delimiters(self):
        checker = self.checker([self.source("a")])
        entry = self.candidates(checker)[0].entry
        entry["release"]["value"] = "a --> <!-- b"
        text = format_metadata(42, "master", "app.json", [entry])
        self.assertEqual(text.count("-->"), 1)
        self.assertEqual(parse_metadata("Human table\n" + text)["updates"], [entry])

    def test_invalid_metadata_fails_publication(self):
        for text in (
            MARKER + "{} -->",
            MARKER + '{"schema":1,"schema":1} -->',
            MARKER + "{}",
            MARKER + "x" * 40001 + " -->",
        ):
            with self.subTest(text=text[:80]), self.assertRaises(MetadataError):
                parse_metadata(text)
        entry = self.candidates(self.checker())[0].entry
        with self.assertRaises(MetadataError):
            format_metadata(42, "master", "app.json", [entry, entry])

    def test_legacy_body_has_no_metadata(self):
        self.assertIsNone(parse_metadata("Update to version 2"))

    def test_publication_records_applied_sources_and_completeness(self):
        for identified in (True, False):
            with self.subTest(identified=identified):
                source = self.source("a")
                if not identified:
                    source["x-checker-data"] = {"type": "json"}
                checker = self.checker([source])
                repo = MagicMock(id=42, default_branch="master", full_name="owner/app")
                repo.owner.login = "owner"
                repo.get_pulls.return_value = []
                with (
                    patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}),
                    patch("src.main.Github") as github,
                    patch(
                        "subprocess.check_output",
                        side_effect=["https://github.com/owner/app\n", "master\n"],
                    ),
                ):
                    github.return_value.get_repo.return_value = repo
                    publication = main.prepare_publication(
                        checker, self.root, str(self.path)
                    )
                checker.update_manifests()
                change = main.CommittedChanges(
                    "Update app", None, "abcdef0", "update-master-abcdef0", "master"
                )
                with (
                    patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}),
                    patch("builtins.open", side_effect=FileNotFoundError),
                    patch("subprocess.run", return_value=SimpleNamespace(returncode=0)),
                ):
                    self.assertTrue(
                        main.open_pr(
                            change, checker, fork=False, publication=publication
                        )
                    )
                metadata = parse_metadata(repo.create_pull.call_args.kwargs["body"])
                self.assertEqual(metadata["repository"], 42)
                self.assertEqual(metadata["manifest"], "app.json")
                self.assertEqual(metadata["complete"], identified)
                self.assertEqual(len(metadata["updates"]), int(identified))
