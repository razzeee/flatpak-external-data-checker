import copy
import unittest
from unittest import mock

from src.checkers.anityachecker import AnityaChecker
from src.lib.errors import SourceLoadError, SourceUpdateError
from src.lib.externaldata import ExternalData


def make_source(source_type="archive", **metadata):
    return ExternalData.from_source(
        "manifest.json",
        {
            "type": source_type,
            "url": "https://example.com/foo-1.0.tar.gz",
            "sha256": "a" * 64,
            "size": 10,
            "mirror-urls": ["https://old.example.com/foo-1.0.tar.gz"],
            "x-checker-data": {"type": "anitya", **metadata},
        },
    )


def set_update(data, version="2.03.4"):
    data.new_version = data.current_version._replace(
        url="https://example.com/foo-2.03.4.tar.gz",
        checksum=data.current_version.checksum._replace(sha256="b" * 64),
        size=20,
        version=version,
    )


class TestMirrorUpdates(unittest.TestCase):
    def test_multiple_mirrors_for_file_source_types(self):
        for source_type in ("archive", "file", "extra-data"):
            with self.subTest(source_type=source_type):
                data = make_source(
                    source_type,
                    **{
                        "mirror-url-templates": [
                            "https://one.example.com/foo-$version.tar.gz",
                            "https://two.example.com/$major.$minor/"
                            "foo-${version0}_${version1}_$patch.tar.gz?cost=$$1",
                        ]
                    },
                )
                source = data.source
                metadata = copy.deepcopy(data.checker_data)
                set_update(data)
                data.update()
                self.assertIs(data.source, source)
                self.assertEqual(source["url"], data.new_version.url)
                self.assertEqual(source["sha256"], "b" * 64)
                self.assertEqual(
                    source["mirror-urls"],
                    [
                        "https://one.example.com/foo-2.03.4.tar.gz",
                        "https://two.example.com/2.03/foo-2_03_4.tar.gz?cost=$1",
                    ],
                )
                self.assertEqual(source["x-checker-data"], metadata)
                if source_type == "extra-data":
                    self.assertEqual(source["size"], 20)
                else:
                    self.assertNotIn("size", source)

    def test_without_opt_in_preserves_existing_mirrors(self):
        for has_mirrors in (True, False):
            with self.subTest(has_mirrors=has_mirrors):
                data = make_source()
                if not has_mirrors:
                    del data.source["mirror-urls"]
                before = copy.deepcopy(data.source)
                set_update(data)
                data.update()
                self.assertEqual(
                    data.source.get("mirror-urls"), before.get("mirror-urls")
                )
                self.assertEqual(data.source["url"], data.new_version.url)

    def test_no_update_leaves_source_unchanged(self):
        data = make_source(**{"mirror-url-templates": ["https://example.com/$bad"]})
        before = copy.deepcopy(data.source)
        data.update()
        self.assertEqual(data.source, before)

    def test_add_and_clear_mirrors(self):
        data = make_source(**{"mirror-url-templates": ["https://example.net/$version"]})
        del data.source["mirror-urls"]
        set_update(data)
        data.update()
        self.assertEqual(data.source["mirror-urls"], ["https://example.net/2.03.4"])
        data.checker_data["mirror-url-templates"] = []
        set_update(data, version=None)
        data.update()
        self.assertEqual(data.source["mirror-urls"], [])

    def test_invalid_template_does_not_partially_update_source(self):
        for template in (
            "https://example.net/$unknown",
            "https://example.net/${version",
            "https://example.net/$version9",
            "https://example.net/$",
            "relative/$version",
            "file:///foo-$version",
            "https:///foo-$version",
            "https://[invalid]/$version",
        ):
            with self.subTest(template=template):
                data = make_source(
                    **{
                        "mirror-url-templates": [
                            "https://valid.example.com/$version",
                            template,
                        ]
                    }
                )
                before = copy.deepcopy(data.source)
                set_update(data)
                with self.assertRaisesRegex(SourceUpdateError, "mirror-url-templates"):
                    data.update()
                self.assertEqual(data.source, before)

    def test_missing_version_leaves_source_unchanged(self):
        data = make_source(**{"mirror-url-templates": ["https://example.net/$version"]})
        before = copy.deepcopy(data.source)
        set_update(data, version=None)
        with self.assertRaisesRegex(SourceUpdateError, "selected version"):
            data.update()
        self.assertEqual(data.source, before)

    def test_checksum_failure_leaves_source_unchanged(self):
        data = make_source(**{"mirror-url-templates": ["https://example.net/$version"]})
        before = copy.deepcopy(data.source)
        set_update(data)
        data.new_version = data.new_version._replace(
            checksum=data.new_version.checksum._replace(sha256=None, sha512="c" * 128)
        )
        with self.assertRaises(SourceUpdateError):
            data.update()
        self.assertEqual(data.source, before)

    def test_missing_extra_data_size_leaves_source_unchanged(self):
        data = make_source(
            "extra-data", **{"mirror-url-templates": ["https://example.net/$version"]}
        )
        before = copy.deepcopy(data.source)
        set_update(data)
        data.new_version = data.new_version._replace(size=None)
        with self.assertRaises(AssertionError):
            data.update()
        self.assertEqual(data.source, before)

    def test_metadata_schema(self):
        for value in (None, "https://example.com/$version", {}, [1], [None], [""]):
            with self.subTest(value=value), self.assertRaises(SourceLoadError):
                make_source(**{"mirror-url-templates": value})


class TestSelectedMirrorVersion(unittest.IsolatedAsyncioTestCase):
    async def test_constraints_select_primary_and_mirror_version(self):
        data = make_source(
            **{
                "project-id": 123,
                "versions": {"<": "3.0"},
                "url-template": "https://example.com/foo-$version.tar.gz",
                "mirror-url-templates": [
                    "https://mirror.example.com/foo-$version.tar.gz"
                ],
            }
        )
        before = copy.deepcopy(data.source)
        response = mock.AsyncMock()
        response.__aenter__.return_value = response
        response.json.return_value = {
            "latest_version": "3.0",
            "stable_versions": ["3.0", "2.03.4", "1.0"],
        }
        session = mock.MagicMock()
        session.get.return_value = response
        checker = AnityaChecker(session)
        await checker.validate_checker_data(data)
        downloaded = data.current_version._replace(
            url="https://example.com/foo-2.03.4.tar.gz",
            checksum=data.current_version.checksum._replace(sha256="b" * 64),
        )
        with mock.patch(
            "src.checkers.utils.get_extra_data_info_from_url",
            new_callable=mock.AsyncMock,
            return_value=downloaded,
        ) as download:
            await checker.check(data)
        self.assertEqual(data.source, before)
        self.assertEqual(data.new_version.version, "2.03.4")
        download.assert_awaited_once()
        self.assertEqual(download.call_args.kwargs["url"], downloaded.url)
        data.update()
        self.assertEqual(data.source["url"], downloaded.url)
        self.assertEqual(data.source["sha256"], "b" * 64)
        self.assertEqual(
            data.source["mirror-urls"],
            ["https://mirror.example.com/foo-2.03.4.tar.gz"],
        )
        session.get.assert_called_once()
