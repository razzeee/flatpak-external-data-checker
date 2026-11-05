import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from github import GithubException

from src import main
from src.lib.checksums import MultiDigest
from src.lib.prhistory import RETRY_LABEL
from src.lib.prmetadata import format_metadata, parse_metadata
from src.lib.updateidentity import release_key
from src.manifest import CheckerOptions, ManifestChecker

from .history_support import HistoryFixture


class SuppressionFixture(HistoryFixture):
    def select(self, checker):
        return self.history.select(
            checker.get_external_data(), self.candidates(checker)
        )


class TestVersionSuppression(SuppressionFixture):
    def test_closed_bundle_filters_each_version_before_writes(self):
        checker = self.checker()
        self.load(self.pr(self.candidates(checker)))
        checker = self.checker()
        (self.root / "README").write_text("unrelated base change")
        a, b = checker.get_external_data()
        b.new_version = b.new_version._replace(version="3")
        allowed = self.select(checker)
        self.assertEqual(allowed, [b])
        changes = checker.update_manifests(allowed)
        self.assertEqual(len(changes), 1)
        sources = json.loads(self.path.read_text())["modules"][0]["sources"]
        self.assertEqual(sources[0]["sha256"], "a" * 64)
        self.assertEqual(sources[1]["sha256"], "b" * 64)
        self.assertEqual(len(checker.source_updates), 1)
        self.assertIsNotNone(a.new_version)

    def test_same_version_new_checksum_stays_rejected(self):
        checker = self.checker([self.source("a")])
        self.load(self.pr(self.candidates(checker)))
        data = checker.get_external_data()[0]
        data.new_version = data.new_version._replace(
            checksum=MultiDigest(sha256="c" * 64), url="https://mirror.example/2"
        )
        with self.assertLogs("src.lib.prhistory", "WARNING"):
            self.assertEqual(self.select(checker), [])
        data.new_version = data.new_version._replace(version="3")
        self.assertEqual(self.select(checker), [data])

    def test_unknown_versions_reject_bytes_not_unknown_forever(self):
        checker = self.checker([self.source("a")])
        data = checker.get_external_data()[0]
        data.new_version = data.new_version._replace(version=None)
        self.load(self.pr(self.candidates(checker)))
        data.new_version = data.new_version._replace(
            url="https://mirror.example/latest"
        )
        self.assertEqual(self.select(checker), [])
        data.new_version = data.new_version._replace(
            checksum=MultiDigest(sha256="c" * 64)
        )
        self.assertEqual(self.select(checker), [data])
        data.new_version = data.new_version._replace(
            checksum=MultiDigest(sha1="d" * 40)
        )
        self.assertIsNone(release_key(data))

    def test_dependency_component_deferred_atomically(self):
        checker = self.checker(
            [self.source("a"), self.source("b", **{"parent-id": "a"}), self.source("c")]
        )
        candidates = self.candidates(checker)
        self.load(self.pr([candidates[1]]))
        self.assertEqual([d.ident for d in self.select(checker)], ["c"])

    def test_suppressed_main_does_not_add_appdata_or_satisfy_importance(self):
        checker = self.checker(
            [self.source("a", **{"is-main-source": True}), self.source("b")],
            important=True,
        )
        self.load(self.pr(self.candidates(checker)[:1]))
        before = self.path.read_bytes()
        with patch("src.manifest.add_release_to_file") as add_release:
            self.assertEqual(checker.update_manifests(self.select(checker)), [])
            add_release.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(checker.get_applied_external_data(), [])

        checker.opts = CheckerOptions()
        appdata = self.root / "org.example.App.metainfo.xml"
        appdata.write_text("<component><id>org.example.App</id></component>")
        with patch("src.manifest.add_release_to_file") as add_release:
            self.assertEqual(len(checker.update_manifests(self.select(checker))), 1)
            add_release.assert_not_called()

    def test_reopen_merged_retry_and_overlapping_rejections(self):
        checker = self.checker([self.source("a")])
        candidates = self.candidates(checker)
        for pr in (
            self.pr(candidates, state="open"),
            self.pr(candidates, merged_at="2026-11-01"),
            self.pr(candidates, labels=[{"name": RETRY_LABEL}]),
        ):
            with self.subTest(pr=pr.raw_data):
                self.load(pr)
                self.assertEqual(len(self.select(checker)), 1)
        self.load(
            self.pr(candidates, labels=[{"name": RETRY_LABEL}]),
            self.pr(candidates, number=2),
        )
        self.assertEqual(self.select(checker), [])
        self.load(self.pr(candidates))  # Label removal/reclose reactivates rejection.
        self.assertEqual(self.select(checker), [])

    def test_pending_source_does_not_block_unrelated_update(self):
        checker = self.checker()
        self.load(self.pr(self.candidates(checker)[:1], state="open"))
        self.assertEqual([d.ident for d in self.select(checker)], ["b"])

    def test_pending_mixed_identity_pr_keeps_whole_proposal(self):
        unknown = self.source("b")
        unknown["x-checker-data"] = {"type": "json"}
        checker = self.checker([self.source("a"), unknown])
        self.load(self.pr(self.candidates(checker), state="open"))
        # Dropping A would create a B-only PR while B is already in the open PR.
        self.assertEqual(self.select(checker), checker.get_external_data())

    def test_previously_untracked_source_does_not_split_pending_pr(self):
        checker = self.checker()
        entries = [c.entry for c in self.candidates(checker)[:1]]
        body = format_metadata(42, "master", "app.json", entries, complete=False)
        self.load(self.pr(self.candidates(checker), state="open", body=body))
        # The original PR includes B but predates its source-id.
        self.assertEqual(self.select(checker), checker.get_external_data())


class TestPreparation(SuppressionFixture):
    def prepare(self, branch, author_ids):
        checker = self.checker()
        self.repo.requester.requestJsonAndCheck.return_value = ({}, [])
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}),
            patch("src.main.Github") as github,
            patch(
                "subprocess.check_output",
                side_effect=["https://github.com/owner/app\n", branch],
            ),
        ):
            github.return_value.get_repo.return_value = self.repo
            github.return_value.get_user.return_value.id = 123
            publication = main.prepare_publication(
                checker, self.root, str(self.path), author_ids
            )
        return publication, github.return_value

    def test_detached_head_and_authenticated_author(self):
        publication, github = self.prepare("", [])
        self.assertEqual(publication.history.base, "master")
        self.assertEqual(publication.history.authors, {123})
        self.assertEqual(publication.history.manifest, "app.json")
        github.get_user.assert_called_once_with()

    def test_explicit_target_and_historical_authors(self):
        publication, github = self.prepare("beta\n", [456, 789])
        self.assertEqual(publication.history.base, "beta")
        self.assertEqual(publication.history.authors, {456, 789})
        github.get_user.assert_not_called()

    def test_unidentified_candidates_skip_history(self):
        with patch.object(ManifestChecker, "get_update_candidates", return_value=[]):
            publication, _ = self.prepare("master", [123])
        self.assertEqual(publication.candidates, [])
        self.repo.requester.requestJsonAndCheck.assert_not_called()


class TestPublication(SuppressionFixture):
    def publish(self, checker, prs):
        self.repo.get_pulls.side_effect = lambda **kw: (
            list(prs)
            if "head" not in kw
            else [p for p in prs if p.raw_data["head"]["label"] == kw["head"]]
        )
        self.repo.requester.requestJsonAndCheck.side_effect = lambda *args, **kw: (
            {},
            [p.raw_data for p in prs],
        )
        self.history.load()
        candidates = self.candidates(checker)
        checker.update_manifests(
            self.history.select(checker.get_external_data(), candidates)
        )
        publication = main.Publication(self.repo, self.history, candidates)
        change = main.CommittedChanges(
            "Update app", None, "abcdef0", "update-master-abcdef0", "master"
        )
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}),
            patch("builtins.open", side_effect=FileNotFoundError),
            patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as push,
        ):
            result = main.open_pr(change, checker, fork=False, publication=publication)
        return result, push

    def test_retry_same_tree_uses_new_deterministic_head(self):
        checker = self.checker([self.source("a")])
        candidates = self.candidates(checker)
        pr = self.pr(
            candidates,
            labels=[{"name": RETRY_LABEL}],
            head={"label": "owner:update-master-abcdef0"},
        )
        result, push = self.publish(checker, [pr])
        self.assertTrue(result)
        head = self.repo.create_pull.call_args.kwargs["head"]
        self.assertTrue(head.startswith("owner:update-master-abcdef0-retry-"))
        branch = head.split(":", 1)[1]
        self.assertIn(
            f"--force-with-lease=refs/heads/{branch}:", push.call_args.args[0]
        )
        metadata = parse_metadata(self.repo.create_pull.call_args.kwargs["body"])
        self.assertEqual(metadata["updates"], [c.entry for c in candidates])

    def test_open_old_tree_is_not_recreated_or_automerged(self):
        checker = self.checker([self.source("a")])
        result, push = self.publish(
            checker, [self.pr(self.candidates(checker), state="open")]
        )
        self.assertTrue(result)
        push.assert_not_called()
        self.repo.create_pull.assert_not_called()

    def test_open_mixed_identity_pr_does_not_create_unknown_only_duplicate(self):
        unknown = self.source("b")
        unknown["x-checker-data"] = {"type": "json"}
        checker = self.checker([self.source("a"), unknown])
        candidates = self.candidates(checker)
        body = format_metadata(
            42, "master", "app.json", [c.entry for c in candidates], complete=False
        )
        result, push = self.publish(
            checker, [self.pr(candidates, state="open", body=body)]
        )
        self.assertTrue(result)
        self.assertEqual(len(checker.get_applied_external_data()), 2)
        push.assert_not_called()
        self.repo.create_pull.assert_not_called()

    def test_only_allowed_entries_are_in_new_pr(self):
        checker = self.checker()
        rejected = self.pr(self.candidates(checker)[:1])
        result, _ = self.publish(checker, [rejected])
        self.assertTrue(result)
        body = self.repo.create_pull.call_args.kwargs["body"]
        metadata = parse_metadata(body)
        self.assertEqual([e["source"]["id"] for e in metadata["updates"]], ["b"])

    def test_suppressed_broken_source_cannot_automerge_unrelated_pr(self):
        source = self.source("a")
        source["type"] = "extra-data"
        checker = self.checker([source, self.source("b")])
        a, b = checker.get_external_data()
        a.state |= a.State.BROKEN
        candidates = self.candidates(checker)
        rejected = self.pr(candidates[:1])
        pending = self.pr(
            candidates[1:],
            state="open",
            number=2,
            head={"label": "owner:update-master-abcdef0"},
        )
        pr = MagicMock(state="open", mergeable=True)
        pr.raw_data = pending.raw_data
        pr.head.repo.get_commit.return_value.get_combined_status.return_value.state = (
            "success"
        )
        result, push = self.publish(checker, [rejected, pr])
        self.assertTrue(result)
        self.assertEqual(checker.get_applied_external_data(), [b])
        pr.merge.assert_not_called()
        push.assert_not_called()

    def test_rejection_during_preparation_aborts_publication(self):
        checker = self.checker([self.source("a")])
        candidates = self.candidates(checker)
        checker.update_manifests()
        publication = main.Publication(self.repo, self.history, candidates)
        self.repo.requester.requestJsonAndCheck.return_value = (
            {},
            [self.pr(candidates).raw_data],
        )
        change = main.CommittedChanges(
            "Update", None, "abc", "update-master-abc", "master"
        )
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "secret"}),
            patch("subprocess.run") as push,
        ):
            self.assertFalse(main.open_pr(change, checker, publication=publication))
        push.assert_not_called()
        self.repo.create_pull.assert_not_called()


class TestSuppressionCLI(SuppressionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_importance_gate_needs_no_publication_context(self):
        checker = self.checker(
            [self.source("a", **{"is-important": True}), self.source("b")],
            important=True,
        )
        checker.get_external_data()[0].new_version = None
        before = self.path.read_bytes()
        with (
            patch("src.main.manifest.ManifestChecker", return_value=checker),
            patch.object(checker, "check", new_callable=AsyncMock),
            patch("src.main.prepare_publication") as prepare,
        ):
            result = await main.run_with_args(
                main.parse_cli_args(["--update", str(self.path)])
            )
        self.assertEqual(result, (1, 0, False))
        self.assertEqual(self.path.read_bytes(), before)
        prepare.assert_not_called()

    async def test_broken_sources_without_candidates_need_no_publication_context(self):
        checker = self.checker()
        for data in checker.get_external_data():
            data.new_version = None
            data.state |= data.State.BROKEN
        with (
            patch("src.main.manifest.ManifestChecker", return_value=checker),
            patch.object(checker, "check", new_callable=AsyncMock),
            patch("src.main.prepare_publication") as prepare,
        ):
            # There is no Git checkout here.
            result = await main.run_with_args(
                main.parse_cli_args(["--update", str(self.path)])
            )
        self.assertEqual(result, (2, 0, False))
        prepare.assert_not_called()

    async def test_history_failure_has_no_local_or_remote_writes(self):
        checker = self.checker()
        before = self.path.read_bytes()
        with (
            patch("src.main.manifest.ManifestChecker", return_value=checker),
            patch.object(checker, "check", new_callable=AsyncMock),
            patch("src.main.get_manifest_git_checkout", return_value=self.root),
            patch("src.main.ensure_git_safe_directory"),
            patch(
                "src.main.prepare_publication",
                side_effect=GithubException(403, "rate limited"),
            ),
            patch("src.main.commit_changes") as commit,
            patch("src.main.open_pr") as publish,
        ):
            result = await main.run_with_args(
                main.parse_cli_args(["--update", str(self.path)])
            )
        self.assertEqual(result, (2, 1, False))
        self.assertEqual(self.path.read_bytes(), before)
        commit.assert_not_called()
        publish.assert_not_called()

    async def test_all_rejected_is_outdated_but_not_error_or_updated(self):
        checker = self.checker()
        candidates = self.candidates(checker)
        self.load(self.pr(candidates))
        publication = main.Publication(self.repo, self.history, candidates)
        before = self.path.read_bytes()
        with (
            patch("src.main.manifest.ManifestChecker", return_value=checker),
            patch.object(checker, "check", new_callable=AsyncMock),
            patch("src.main.get_manifest_git_checkout", return_value=self.root),
            patch("src.main.ensure_git_safe_directory"),
            patch("src.main.prepare_publication", return_value=publication),
            patch("src.main.commit_changes") as commit,
        ):
            result = await main.run_with_args(
                main.parse_cli_args(["--update", "--check-outdated", str(self.path)])
            )
        self.assertEqual(result, (2, 0, False))
        self.assertEqual(self.path.read_bytes(), before)
        commit.assert_not_called()

    async def test_edit_only_does_not_read_history(self):
        checker = self.checker()
        with (
            patch("src.main.manifest.ManifestChecker", return_value=checker),
            patch.object(checker, "check", new_callable=AsyncMock),
            patch("src.main.prepare_publication") as prepare,
        ):
            result = await main.run_with_args(
                main.parse_cli_args(["--edit-only", str(self.path)])
            )
        self.assertTrue(result[2])
        prepare.assert_not_called()
