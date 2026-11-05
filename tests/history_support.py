from types import SimpleNamespace
from unittest.mock import MagicMock

from src.lib.prhistory import PRHistory
from src.lib.prmetadata import format_metadata

from .update_support import UpdateFixture


class HistoryFixture(UpdateFixture):
    def setUp(self):
        super().setUp()
        self.repo = MagicMock(id=42, default_branch="master", full_name="owner/app")
        self.repo.owner.login = "owner"
        self.history = PRHistory(self.repo, "master", "app.json", {123})

    def pr(self, candidates, state="closed", number=1, **changes):
        raw = {
            "number": number,
            "html_url": f"https://github.com/owner/app/pull/{number}",
            "user": {"id": 123},
            "body": format_metadata(
                42, "master", "app.json", [c.entry for c in candidates]
            ),
            "base": {"ref": "master"},
            "head": {"label": "owner:update-old-tree"},
            "state": state,
            "merged_at": None,
            "labels": [],
        }
        raw.update(changes)
        return SimpleNamespace(raw_data=raw, html_url=raw["html_url"])

    def load(self, *prs):
        self.repo.requester.requestJsonAndCheck.return_value = (
            {},
            [p.raw_data for p in prs],
        )
        self.history.load()
