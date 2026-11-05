import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock, patch

from github import GithubException

from src.lib.prhistory import RETRY_LABEL, HistoryError, PRHistory
from src.lib.prmetadata import MARKER

from .history_support import HistoryFixture


class TestHistoryCache(HistoryFixture):
    def setUp(self):
        super().setUp()
        self.history.cache_dir = self.root / "cache"
        self.entries = self.candidates(self.checker())
        self.request = self.repo.requester.requestJsonAndCheck
        self.headers = {"date": "Wed, 04 Nov 2026 11:00:00 GMT"}
        self.next_page = {
            **self.headers,
            "link": '<https://api.github.com/next>; rel="next"',
        }

    def raw(self, number=1, updated="2026-11-04T11:00:00Z", **changes):
        return self.pr(
            self.entries, number=number, updated_at=updated, **changes
        ).raw_data

    def seed(self):
        self.request.return_value = (self.headers, [self.raw()])
        self.history.load()
        self.request.reset_mock()
        return next(self.history.cache_dir.glob("*.json"))

    def test_cold_scan_then_warm_scan_keeps_old_rejections(self):
        old = self.raw(2, "2020-01-01T00:00:00Z")
        self.request.side_effect = [
            (self.next_page, [self.raw()]),
            (self.headers, [old]),
        ]
        self.history.load()
        self.assertEqual(self.request.call_count, 2)
        self.request.reset_mock()
        self.request.side_effect = None
        self.request.return_value = (self.next_page, [self.raw(), old])
        fresh = PRHistory(
            self.repo, "master", "app.json", {123}, cache_dir=self.history.cache_dir
        )
        fresh.load()
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual({r.number for r in fresh.records}, {1, 2})
        self.assertNotIn("base", self.request.call_args.kwargs["parameters"])
        self.assertEqual(self.request.call_args.kwargs["parameters"]["sort"], "updated")

    def test_refresh_changes_rejection_state(self):
        self.seed()
        for changes, expected in [
            ({"state": "open"}, [("open", False)]),
            ({"labels": [{"name": RETRY_LABEL}]}, [("closed", True)]),
            ({}, [("closed", False)]),
            ({"merged_at": "2026-11-04T11:01:00Z"}, []),
            ({"base": {"ref": "beta"}}, []),
            ({"body": "metadata removed"}, []),
        ]:
            with self.subTest(changes=changes):
                self.request.return_value = (self.headers, [self.raw(**changes)])
                self.history.load()
                self.assertEqual(
                    [(r.state, r.retry) for r in self.history.records], expected
                )

    def test_new_pr_and_timestamp_overlap_on_later_page(self):
        self.seed()
        self.request.side_effect = [
            (self.next_page, [self.raw(3, "2026-11-04T11:01:00Z")]),
            (self.next_page, [self.raw(2, "2026-11-04T10:59:00Z")]),
            (self.headers, [self.raw(4, "2020-01-01T00:00:00Z")]),
        ]
        self.history.load()
        self.assertEqual({r.number for r in self.history.records}, {1, 2, 3})
        self.assertEqual(self.request.call_count, 3)

    def test_failed_refresh_preserves_cache(self):
        path = self.seed()
        previous = path.read_bytes()
        self.request.side_effect = [
            (self.next_page, [self.raw(state="open")]),
            GithubException(429, "rate limited"),
        ]
        with self.assertRaises(GithubException):
            self.history.load()
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.history.records[0].state, "closed")

    def test_invalid_metadata_preserves_cache(self):
        path = self.seed()
        previous = path.read_bytes()
        self.request.return_value = (self.headers, [self.raw(body=MARKER + "bad")])
        with self.assertRaises(HistoryError):
            self.history.load()
        self.assertEqual(path.read_bytes(), previous)

    def test_corrupt_cache_requires_full_scan(self):
        path = self.seed()
        for text in ("{", "null", '{"schema":2}'):
            with self.subTest(text=text):
                path.write_text(text)
                self.request.side_effect = [
                    (self.next_page, [self.raw()]),
                    (self.headers, [self.raw(2, "2020-01-01T00:00:00Z")]),
                ]
                self.history.load()
                self.assertEqual({r.number for r in self.history.records}, {1, 2})

    def test_missing_timestamp_does_not_end_scan_silently(self):
        path = self.seed()
        previous = path.read_bytes()
        raw = self.raw()
        del raw["updated_at"]
        self.request.return_value = (self.headers, [raw])
        with self.assertRaises(HistoryError):
            self.history.load()
        self.assertEqual(path.read_bytes(), previous)

    def test_watermark_does_not_pass_scan_start(self):
        self.request.return_value = (
            self.headers,
            [self.raw(updated="2026-11-04T12:30:00Z")],
        )
        self.history.load()
        path = next(self.history.cache_dir.glob("*.json"))
        self.assertEqual(
            json.loads(path.read_text())["updated_at"], "2026-11-04T11:00:00Z"
        )

    def test_interrupted_write_preserves_cache(self):
        path = self.seed()
        previous = path.read_bytes()
        with patch("src.lib.prhistory.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.history.load()
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(len(list(path.parent.iterdir())), 2)

    def test_concurrent_refreshes_keep_both_updates(self):
        path = self.seed()
        entered = Event()
        second_started = Event()

        def first_request(*args, **kwargs):
            entered.set()
            self.assertTrue(second_started.wait(5))
            return self.headers, [self.raw(2, "2026-11-04T11:01:00Z")]

        self.request.side_effect = first_request
        other_repo = MagicMock(id=42, url=self.repo.url)
        other_repo.requester.requestJsonAndCheck.return_value = (
            self.headers,
            [self.raw(3, "2026-11-04T11:02:00Z")],
        )
        other = PRHistory(
            other_repo, "master", "app.json", {123}, cache_dir=self.history.cache_dir
        )

        def second_load():
            self.assertTrue(entered.wait(5))
            second_started.set()
            other.load()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.history.load)
            second = pool.submit(second_load)
            first.result(timeout=10)
            second.result(timeout=10)
        self.assertEqual(set(json.loads(path.read_text())["pulls"]), {"1", "2", "3"})

    def test_author_sets_have_separate_caches(self):
        self.seed()
        other = PRHistory(
            self.repo, "master", "app.json", {456}, cache_dir=self.history.cache_dir
        )
        other.load()
        self.assertEqual(other.records, [])
        self.assertEqual(len(list(self.history.cache_dir.glob("*.json"))), 2)
