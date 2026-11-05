from github import GithubException

from src.lib.prhistory import HistoryError
from src.lib.prmetadata import MARKER, format_metadata

from .history_support import HistoryFixture


class TestHistory(HistoryFixture):
    def test_legacy_and_untrusted_markers_ignored(self):
        candidates = self.candidates(self.checker())
        self.load(
            self.pr(candidates, body="legacy body"),
            self.pr(candidates, user={"id": 999}, body=MARKER + "invalid"),
        )
        self.assertEqual(self.history.records, [])

    def test_full_pagination_and_no_per_pr_api_calls(self):
        candidates = self.candidates(self.checker())
        pulls = [
            self.pr(
                candidates,
                number=number,
                body="legacy"
                if number < 205
                else format_metadata(
                    42, "master", "app.json", [c.entry for c in candidates]
                ),
            ).raw_data
            for number in range(1, 206)
        ]
        next_page = {
            "link": '<https://api.github.com/repos/owner/app/pulls?page=2>; rel="next"'
        }
        request = self.repo.requester.requestJsonAndCheck
        request.side_effect = [
            (next_page, pulls[:100]),
            (next_page, pulls[100:200]),
            ({}, pulls[200:]),
        ]
        self.history.load()
        self.assertEqual([r.number for r in self.history.records], [205])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(
            [call.kwargs["parameters"]["page"] for call in request.call_args_list],
            [1, 2, 3],
        )
        self.repo.get_pull.assert_not_called()

    def test_scope_and_merge_state_validated(self):
        candidates = self.candidates(self.checker())
        self.load(
            self.pr(
                candidates,
                body=format_metadata(
                    42, "master", "other.json", [c.entry for c in candidates]
                ),
            )
        )
        self.assertEqual(self.history.records, [])
        for overrides in (
            {"body": format_metadata(42, "beta", "app.json", [])},
            {"body": MARKER + "invalid"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(HistoryError):
                self.load(self.pr(candidates, **overrides))
        pr = self.pr(candidates)
        del pr.raw_data["merged_at"]
        with self.assertRaises(HistoryError):
            self.load(pr)

    def test_failed_later_page_does_not_install_partial_history(self):
        candidates = self.candidates(self.checker())
        self.load(self.pr(candidates))
        previous = self.history.records
        self.repo.requester.requestJsonAndCheck.side_effect = [
            ({"link": '<https://api.github.com/next>; rel="next"'}, []),
            GithubException(429, "rate limited"),
        ]
        with self.assertRaises(GithubException):
            self.history.load()
        self.assertIs(self.history.records, previous)
