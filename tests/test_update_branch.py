import os
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src import main
from src.lib.prreplacement import replacement_marker, supersedes
from src.lib.sourceupdates import SourceUpdate


class TestUpdateBranch(unittest.TestCase):
    def setUp(self):
        self.change = main.CommittedChanges(
            "Update foo",
            None,
            "9416508" + "0" * 33,
            "update-master-9416508",
            "master",
        )
        self.repo = MagicMock()
        self.repo.full_name = "owner/repo"
        self.repo.owner.login = "owner"
        self.repo.default_branch = "master"
        self.repo.get_pulls.return_value = []

    def run_open_pr(
        self, returncode=0, stdout="", stderr="", checker=None, revision="a" * 40
    ):
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "test-secret"}),
            patch("src.main.Github") as github,
            patch("builtins.open", side_effect=FileNotFoundError),
            patch(
                "subprocess.check_output",
                side_effect=lambda cmd, **kw: (
                    f"{revision} {'c' * 40}"
                    if "rev-list" in cmd
                    else b"https://github.com/owner/repo"
                ),
            ),
            patch(
                "subprocess.check_call",
                side_effect=subprocess.CalledProcessError(1, "git push"),
            ),
            patch(
                "subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [], returncode, stdout, stderr
                ),
            ) as run,
        ):
            github.return_value.get_repo.return_value = self.repo
            result = main.open_pr(self.change, checker, fork=False)
        return result, run

    def test_new_branch(self):
        result, run = self.run_open_pr()
        self.assertTrue(result)
        run.assert_called_once_with(
            [
                "git",
                "push",
                "--porcelain",
                "--force-with-lease=refs/heads/update-master-9416508:",
                "https://x-access-token:test-secret@github.com/owner/repo",
                "HEAD:refs/heads/update-master-9416508",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.repo.create_pull.assert_called_once()

    def check_collision(self, status):
        with self.assertLogs("src.main", "ERROR") as logs:
            result, run = self.run_open_pr(
                1,
                f"!\tHEAD:refs/heads/update-master-9416508\t{status}\n",
            )
        self.assertFalse(result)
        run.assert_called_once()
        self.assertIn(
            "--force-with-lease=refs/heads/update-master-9416508:",
            run.call_args.args[0],
        )
        self.repo.create_pull.assert_not_called()
        self.assertIn("update-master-9416508", " ".join(logs.output))
        self.assertIn("inspect", " ".join(logs.output))

    def test_orphan_branch(self):
        self.check_collision("[rejected] (stale info)")

    def test_concurrent_branch_creation(self):
        # Another writer creates the ref after the server advertises its refs.
        self.check_collision("[remote rejected] (failed to update ref)")

    def test_unrelated_push_error(self):
        with self.assertLogs("src.main", "ERROR") as logs:
            result, run = self.run_open_pr(
                128,
                stderr="fatal: authentication failed for "
                "https://x-access-token:test-secret@github.com/owner/repo",
            )
        self.assertFalse(result)
        run.assert_called_once()
        self.repo.create_pull.assert_not_called()
        self.assertIn("authentication failed", " ".join(logs.output))
        self.assertNotIn("test-secret", " ".join(logs.output))

    def test_existing_prs_still_deduplicate(self):
        for state in ("open", "closed"):
            self.repo.get_pulls.return_value = [MagicMock(state=state)]
            result, run = self.run_open_pr()
            self.assertTrue(result)
            run.assert_not_called()
            self.repo.create_pull.assert_not_called()

    def test_replacement_closes_only_covered_untouched_prs(self):
        checker = MagicMock()
        checker.source_updates = [SourceUpdate("foo", "1", "2", None)]
        checker.pr_targets = {"foo": ("2", "1" * 64)}
        self.run_open_pr(checker=checker)
        body = self.repo.create_pull.call_args.kwargs["body"]
        old = MagicMock(state="open", body=body)
        old.head.repo = self.repo
        old.head.ref = "update-master-1234567"
        old.head.sha = "a" * 40
        old.base.ref = "master"
        old.user.id = self.repo.create_pull.return_value.user.id
        self.repo.get_pull.return_value = old
        for mismatch in (
            "version",
            "author",
            "edited",
            "missing",
            "legacy",
            "push",
            None,
        ):
            with self.subTest(mismatch=mismatch):
                old.body = "" if mismatch == "legacy" else body
                old.head.sha = "d" * 40 if mismatch == "edited" else "a" * 40
                old.user.id = (
                    42
                    if mismatch == "author"
                    else self.repo.create_pull.return_value.user.id
                )
                checker.pr_targets = {
                    "bar" if mismatch == "missing" else "foo": (
                        "1" if mismatch == "version" else "10",
                        "2" * 64,
                    )
                }
                self.repo.get_pulls.side_effect = [[], [old]]
                self.run_open_pr(
                    int(mismatch == "push"), checker=checker, revision="b" * 40
                )
                if mismatch:
                    old.edit.assert_not_called()
                    old.create_issue_comment.assert_not_called()
                else:
                    old.edit.assert_called_once_with(state="closed")
                    old.create_issue_comment.assert_called_once_with(
                        f"Superseded by {self.repo.create_pull.return_value.html_url}."
                    )

    def test_replacement_must_cover_every_source(self):
        first, second, head = "1" * 64, "2" * 64, "a" * 40
        previous = {first: ("1.9", "a" * 64), second: ("unknown", "b" * 64)}
        body = replacement_marker(head, previous)
        newer = {first: ("1.10", "c" * 64)}
        self.assertFalse(supersedes(body, head, previous))
        self.assertFalse(supersedes(body, head, newer))
        self.assertFalse(
            supersedes(body, head, newer | {second: ("unknown", "d" * 64)})
        )
        self.assertTrue(supersedes(body, head, previous | newer))


class TestPublicationFailure(unittest.IsolatedAsyncioTestCase):
    async def test_failure_is_reported_to_entrypoint(self):
        with (
            patch("src.main.manifest.ManifestChecker") as factory,
            patch("src.main.init_logging"),
            patch("src.main.is_end_of_life", return_value=False),
            patch("src.main.print_outdated_external_data", return_value=1),
            patch("src.main.get_manifest_git_checkout"),
            patch("src.main.ensure_git_safe_directory"),
            patch("src.main.indir"),
            patch("src.main.commit_changes"),
            patch("src.main.open_pr", return_value=False),
        ):
            checker = factory.return_value
            checker.check = AsyncMock()
            checker.update_manifests.return_value = ["Update foo"]
            result = await main.run_with_args(
                main.parse_cli_args(["--update", "/fake/manifest.json"])
            )
        self.assertEqual(result, (-1, -1, False))
