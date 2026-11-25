import datetime
import os
import subprocess
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from github import GithubException

from src import main
from src.lib.prreplacement import Target, replacement_marker, supersedes


class PublicationFixture(unittest.TestCase):
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


class TestUpdateBranch(PublicationFixture):
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

    def test_replacement_must_cover_every_source(self):
        first, second, head = "1" * 64, "2" * 64, "a" * 40
        previous = {first: Target("1.9", "a" * 64), second: Target("unknown", "b" * 64)}
        body = replacement_marker(head, previous)
        newer = {first: Target("1.10", "c" * 64)}
        self.assertFalse(supersedes(body, head, previous))
        self.assertFalse(supersedes(body, head, newer))
        self.assertFalse(
            supersedes(body, head, newer | {second: Target("unknown", "d" * 64)})
        )
        self.assertTrue(supersedes(body, head, previous | newer))


class TestCloseSuperseded(PublicationFixture):
    def setUp(self):
        super().setUp()
        self.key = "1" * 64
        self.old = MagicMock(state="open", number=1, commits=1)
        self.old.head.repo = self.repo
        self.old.head.ref = "update-master-1234567"
        self.old.head.sha = "a" * 40
        self.old.base.ref = "master"
        self.old.user.id = 123
        self.old.created_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
        self.old.body = replacement_marker(
            self.old.head.sha, {self.key: Target("rotating", "2" * 64)}
        )
        self.repo.get_pull.return_value = self.old
        self.repo.get_pulls.side_effect = [[], [self.old]]
        self.new = self.repo.create_pull.return_value
        self.new.number = 2
        self.new.commits = 1
        self.new.user.id = 123
        self.new.base.ref = "master"
        self.new.head.sha = "b" * 40
        self.checker = MagicMock()
        self.checker.root_manifest_file = "app.yaml"
        self.checker.opts.max_manifest_size = 1024
        self.checker.check_started = self.old.created_at + datetime.timedelta(days=1)
        self.checker.pr_targets = {self.key: Target("rotating", "3" * 64)}

    def publish(self, returncode=0):
        return self.run_open_pr(
            returncode, checker=self.checker, revision=self.new.head.sha
        )

    def test_retry_after_close_failure(self):
        self.old.edit.side_effect = [GithubException(503, None, None), None]
        with self.assertLogs("src.main", "WARNING"):
            self.publish()
        self.new.body = self.repo.create_pull.call_args.kwargs["body"]
        self.new.state = "open"
        self.repo.get_pulls.side_effect = [[self.new], [self.old]]
        self.publish()
        self.assertEqual(self.old.edit.call_count, 2)
        self.assertIn(
            f"Superseded by {self.new.html_url}.",
            self.old.edit.call_args.kwargs["body"],
        )
        self.repo.create_pull.assert_called_once()

    def test_newer_observation_required(self):
        self.old.created_at = self.checker.check_started
        self.publish()
        self.old.edit.assert_not_called()

    def test_existing_rotating_replacement_published_first(self):
        self.old.number = 3
        self.new.body = replacement_marker(self.new.head.sha, self.checker.pr_targets)
        self.new.state = "open"
        self.repo.get_pulls.side_effect = [[self.new], [self.old]]
        self.publish()
        self.assertEqual(self.old.edit.call_args.kwargs["state"], "closed")
        self.repo.create_pull.assert_not_called()

    def test_push_failure_leaves_old_open(self):
        self.publish(1)
        self.old.edit.assert_not_called()

    def test_edited_head_is_kept(self):
        self.old.head.sha = "d" * 40
        self.publish()
        self.old.edit.assert_not_called()

    def test_other_author_is_kept(self):
        self.old.user.id = 42
        self.publish()
        self.old.edit.assert_not_called()

    def setup_legacy_pr(self):
        self.old.body = main.DISCLAIMER
        commit = self.repo.get_commit.return_value
        commit.commit.tree.sha = "1234567" + "d" * 33
        commit.parents = [MagicMock(sha="e" * 40)]
        commit.commit.message = "foo: Update foo to 1.2"
        commit.files = [MagicMock(filename="app.yaml", status="modified")]
        self.old.changed_files = 1
        self.repo.get_contents.return_value.sha = "f" * 40
        self.repo.get_contents.return_value.size = 2
        self.repo.get_contents.return_value.encoding = "base64"
        self.repo.get_contents.return_value.decoded_content = b"{}\n"
        self.repo.get_git_tree.return_value.truncated = False
        self.repo.get_git_tree.return_value.tree = [
            MagicMock(path="app.yaml", mode="100644")
        ]

    def test_legacy_pr_across_unrelated_base_change(self):
        self.setup_legacy_pr()
        self.publish()
        self.checker.covers_legacy_update.assert_called_once_with(
            {"app.yaml"},
            ["foo: Update foo to 1.2"],
            {},
            replay={},
            later_observation=True,
        )
        self.assertEqual(self.old.edit.call_args.kwargs["state"], "closed")

    def test_dirty_update_cannot_be_adopted_as_legacy(self):
        self.setup_legacy_pr()
        self.old.body = f"{main.KEEP_OPEN}\n\n{main.DISCLAIMER}"
        self.publish()
        self.old.edit.assert_not_called()

    def test_legacy_packaging_edit_is_not_covered(self):
        self.setup_legacy_pr()
        base = MagicMock(size=3, encoding="base64", decoded_content=b"{}\n")
        head = MagicMock(
            size=20, encoding="base64", decoded_content=b"buildsystem: simple\n"
        )
        self.repo.get_contents.side_effect = [base, base, base, head]
        self.publish()
        self.old.edit.assert_not_called()

    def test_later_publication_of_older_numeric_release(self):
        self.old.number = self.new.number + 1
        self.old.body = replacement_marker(
            self.old.head.sha, {self.key: Target("2.0", "2" * 64)}
        )
        self.checker.pr_targets = {self.key: Target("3.0", "3" * 64)}
        self.publish()
        self.assertEqual(self.old.edit.call_args.kwargs["state"], "closed")

    def test_legacy_pr_with_changed_manifest_base_is_kept(self):
        self.setup_legacy_pr()
        self.repo.get_contents.side_effect = [
            MagicMock(sha="1" * 40),
            MagicMock(sha="2" * 40),
        ]
        self.publish()
        self.old.edit.assert_not_called()

    def test_legacy_pr_with_followup_commit_is_kept(self):
        self.setup_legacy_pr()
        self.old.commits = 2
        self.publish()
        self.old.edit.assert_not_called()

    def test_marked_pr_with_extra_commit_is_kept(self):
        self.old.commits = 2
        self.publish()
        self.old.edit.assert_not_called()

    def test_legacy_pr_with_mode_change_is_kept(self):
        self.setup_legacy_pr()
        self.repo.get_git_tree.side_effect = [
            MagicMock(truncated=False, tree=[MagicMock(path="app.yaml", mode=mode)])
            for mode in ("100644", "100755")
        ]
        self.publish()
        self.old.edit.assert_not_called()

    def test_fallback_marker_fits_and_keeps_update_heading(self):
        self.checker.pr_targets = {}
        self.checker.source_updates = []
        self.change = self.change._replace(body="a" * main.MAX_PR_BODY_LENGTH)
        self.publish()
        body = self.repo.create_pull.call_args.kwargs["body"]
        self.assertTrue(body.startswith("### Updates"))
        self.assertTrue(body.endswith(main.KEEP_OPEN))
        self.assertEqual(len(body), main.MAX_PR_BODY_LENGTH)


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
