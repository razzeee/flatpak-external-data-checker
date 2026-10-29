import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src import main
from src.lib.errors import SourceLoadError
from src.lib.externaldata import ExternalBase
from src.manifest import ManifestChecker


class TestChangelog(unittest.TestCase):
    url = "https://example.org/news"

    def source(self, kind="file", metadata=None):
        source = {
            "type": kind,
            "url": "https://example.org/source",
            "x-checker-data": {"type": "git" if kind == "git" else "pypi"},
        }
        if kind == "git":
            source.update(commit="old", tag="v1")
        else:
            source["sha256"] = "old"
        source["x-checker-data"].update(metadata or {})
        return source

    def test_load_changelog(self):
        for kind in ("file", "git"):
            for metadata, expected in (
                ({}, None),
                ({"changelog-url": self.url}, self.url),
                ({"changelog-url-template": self.url}, self.url),
                ({"changelog-url": ""}, ""),
                ({"changelog-url": "", "changelog-url-template": self.url}, ""),
                (
                    {"changelog-url": self.url, "changelog-url-template": "old"},
                    self.url,
                ),
            ):
                with self.subTest(kind=kind, metadata=metadata):
                    data = ExternalBase.from_source(
                        "test.json", self.source(kind, metadata)
                    )
                    self.assertEqual(data.current_version.changelog_url, expected)
                    self.assertEqual(
                        data.current_version.json["changelog_url"], expected
                    )

    def test_invalid_changelog(self):
        for key in ("changelog-url", "changelog-url-template"):
            for value in (None, 42, [], "https://example.org/\n", "a\rb"):
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaises(SourceLoadError),
                ):
                    ExternalBase.from_source(
                        "test.json", self.source(metadata={key: value})
                    )

    def test_propagate_to_updates_and_repairs(self):
        for kind in ("file", "git"):
            for is_update in (True, False):
                for same_version in (True, False):
                    with self.subTest(
                        kind=kind, is_update=is_update, same_version=same_version
                    ):
                        data = ExternalBase.from_source(
                            "test.json", self.source(kind, {"changelog-url": self.url})
                        )
                        changes = {"changelog_url": None}
                        if kind == "git":
                            changes.update(
                                commit="new", tag="v1" if same_version else "v2"
                            )
                        else:
                            changes.update(
                                checksum=data.current_version.checksum._replace(
                                    sha256="new"
                                ),
                                url=data.current_version.url
                                if same_version
                                else "https://example.org/v2",
                            )
                        data.set_new_version(
                            data.current_version._replace(**changes),
                            is_update=is_update,
                        )
                        self.assertEqual(data.new_version.changelog_url, self.url)

    def test_candidate_link_takes_precedence(self):
        data = ExternalBase.from_source(
            "test.json", self.source("git", {"changelog-url": self.url})
        )
        candidate_url = "https://example.org/releases/v2"
        data.set_new_version(
            data.current_version._replace(
                commit="new", tag="v2", changelog_url=candidate_url
            )
        )
        self.assertEqual(data.new_version.changelog_url, candidate_url)

    def test_changelog_does_not_trigger_update(self):
        data = ExternalBase.from_source("test.json", self.source("git"))
        data.set_new_version(data.current_version._replace(changelog_url=self.url))
        self.assertIsNone(data.new_version)
        self.assertIn(data.State.LATEST, data.state)

    def test_manifest_and_console(self):
        for kind in ("file", "git"):
            for url in (None, "", self.url, "https://example.org/$version"):
                with (
                    self.subTest(kind=kind, url=url),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    path = Path(directory) / "module.json"
                    metadata = {"changelog-url": url} if url is not None else {}
                    path.write_text(
                        json.dumps(
                            {"name": "foo", "sources": [self.source(kind, metadata)]}
                        )
                    )
                    checker = ManifestChecker(str(path))
                    data = checker.get_external_data()[0]
                    candidate = data.current_version._replace(
                        url="https://example.org/v2", version="2"
                    )
                    if kind == "git":
                        candidate = candidate._replace(commit="new", tag="v2")
                    else:
                        candidate = candidate._replace(
                            checksum=candidate.checksum._replace(sha256="new")
                        )
                    data.set_new_version(candidate)
                    with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                        main.print_outdated_external_data(checker)
                    output = stdout.getvalue()
                    self.assertTrue(output.endswith("\n\n"))
                    changes = checker.update_manifests()
                    expected = "foo: Update source to 2"
                    if url:
                        expected += f"\nChangelog: {url}"
                        self.assertIn(f"  Changelog: {url}\n", output)
                    else:
                        self.assertNotIn("Changelog:", output)
                    self.assertEqual(changes, [expected])
                    self.assertEqual(
                        json.loads(path.read_text())["sources"][0]["x-checker-data"],
                        self.source(kind, metadata)["x-checker-data"],
                    )

    def test_broken_source_without_replacement(self):
        data = ExternalBase.from_source(
            "test.json", self.source(metadata={"changelog-url": self.url})
        )
        data.state = data.State.BROKEN
        checker = MagicMock()
        checker.get_outdated_external_data.return_value = [data]
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(main.print_outdated_external_data(checker), 1)
        self.assertEqual(
            stdout.getvalue(),
            "BROKEN: source\n Couldn't get new version for https://example.org/source\n\n",
        )

    def test_commit_and_pr_messages(self):
        linked = f"foo: Update source to 2\nChangelog: {self.url}"
        for changes, subject, body in (
            ([linked], "foo: Update source to 2", f"Changelog: {self.url}"),
            (
                [linked, "foo: Update other"],
                "Update foo module",
                linked + "\n\nfoo: Update other",
            ),
            (
                [linked, "bar: Update other"],
                "Update foo and bar modules",
                linked + "\n\nbar: Update other",
            ),
            (["foo: Update source"], "foo: Update source", None),
            (
                ["foo: Update source", "bar: Update other"],
                "Update foo and bar modules",
                "foo: Update source\nbar: Update other",
            ),
        ):
            with (
                self.subTest(changes=changes),
                patch("src.main.check_call") as check_call,
                patch(
                    "src.main.subprocess.check_output",
                    side_effect=["master\n", "abcdef0123\n"],
                ),
            ):
                result = main.commit_changes(changes, commit_to_base=True)
                self.assertEqual(result.subject, subject)
                self.assertEqual(result.body, body)
                check_call.assert_called_once_with(
                    ["git", "commit", "-am", subject + ("\n\n" + body if body else "")]
                )
            with (
                patch.dict(
                    "os.environ",
                    {
                        "GITHUB_TOKEN": "test-token",
                        "GITHUB_RUN_ID": "",
                        "GITHUB_REPOSITORY": "",
                    },
                ),
                patch("src.main.Github") as github,
                patch("src.main.check_call"),
                patch(
                    "src.main.subprocess.check_output",
                    return_value=b"https://github.com/example/repo\n",
                ),
                patch("builtins.open", side_effect=FileNotFoundError),
            ):
                repo = MagicMock()
                repo.default_branch = "master"
                repo.get_pulls.return_value = []
                github.return_value.get_repo.return_value = repo
                main.open_pr(result, fork=False)
                self.assertEqual(repo.create_pull.call_args.kwargs["title"], subject)
                self.assertEqual(
                    repo.create_pull.call_args.kwargs["body"],
                    ((body or "") + "\n\n" + main.DISCLAIMER).strip(),
                )
