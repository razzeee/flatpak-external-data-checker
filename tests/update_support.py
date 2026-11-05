import json
import tempfile
import unittest
from pathlib import Path

from src.lib.checksums import MultiDigest
from src.manifest import CheckerOptions, ManifestChecker


class UpdateFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "app.json"

    def checker(self, sources=None, important=False):
        if sources is None:
            sources = [self.source("a"), self.source("b")]
        self.path.write_text(
            json.dumps(
                {
                    "id": "org.example.App",
                    "modules": [{"name": "app", "sources": sources}],
                }
            )
        )
        checker = ManifestChecker(
            str(self.path), CheckerOptions(require_important_update=important)
        )
        for data in checker.get_external_data():
            data.new_version = data.current_version._replace(
                url="https://example.com/2.tar.gz",
                version="2",
                checksum=MultiDigest(sha256="b" * 64),
            )
        return checker

    @staticmethod
    def source(ident, **metadata):
        return {
            "type": "archive",
            "url": f"https://example.com/{ident}-1.tar.gz",
            "sha256": "a" * 64,
            "x-checker-data": {
                "type": "rotating-url",
                "url": "https://example.com/latest",
                "source-id": ident,
                **metadata,
            },
        }

    def candidates(self, checker):
        return checker.get_update_candidates(self.root)
